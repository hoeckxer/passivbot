import asyncio
import hashlib
import hmac
import json
from time import time
from urllib.parse import urlencode

import aiohttp
import numpy as np
from dateutil import parser

from passivbot import ts_to_date, print_, Bot, sort_dict_keys
from jitted import calc_long_pnl, calc_shrt_pnl


def first_capitalized(s: str):
    return s[0].upper() + s[1:].lower()


def format_tick(tick: dict) -> dict:
    return {'trade_id': int(tick['id']),
            'price': float(tick['price']),
            'qty': float(tick['qty']),
            'timestamp': date_to_ts(tick['time']),
            'is_buyer_maker': tick['side'] == 'Sell'}


async def fetch_ticks(cc, symbol: str, from_id: int = None, do_print=True) -> [dict]:
    params = {'symbol': symbol, 'limit': 1000}
    if from_id:
        params['from'] = max(0, from_id)
    try:
        fetched_trades = await cc.v2_public_get_trading_records(params=params)
    except Exception as e:
        print(e)
        return []
    trades = [format_tick(t) for t in fetched_trades['result']]
    if do_print:
        print_(['fetched trades', symbol, trades[0]['trade_id'],
                ts_to_date(trades[0]['timestamp'] / 1000)])
    return trades


def date_to_ts(date: str):
    return parser.parse(date).timestamp() * 1000


class Bybit(Bot):
    def __init__(self, config: dict):
        self.exchange = 'bybit'
        self.min_notional = 0.0
        super().__init__(config)
        self.base_endpoint = 'https://api.bybit.com'
        self.endpoints = {}
        self.market_type = ''
        self.session = aiohttp.ClientSession()

    def init_market_type(self):
        if self.symbol.endswith('USDT'):
            print('linear perpetual')
            self.market_type = 'linear_perpetual'
            self.inverse = self.config['inverse'] = False
            self.endpoints = {'position': '/private/linear/position/list',
                              'open_orders': '/private/linear/order/search',
                              'create_order': '/private/linear/order/create',
                              'cancel_order': '/private/linear/order/cancel',
                              'ticks': '/public/linear/recent-trading-records',
                              'websocket': 'wss://stream.bybit.com/realtime_public',
                              'created_at_key': 'created_time'}

        else:
            self.inverse = self.config['inverse'] = True
            if self.symbol.endswith('USD'):
                print('inverse perpetual')
                self.market_type = 'inverse_perpetual'
                self.endpoints = {'position': '/v2/private/position/list',
                                  'open_orders': '/v2/private/order',
                                  'create_order': '/v2/private/order/create',
                                  'cancel_order': '/v2/private/order/cancel',
                                  'ticks': '/v2/public/trading-records',
                                  'websocket': 'wss://stream.bybit.com/realtime',
                                  'created_at_key': 'created_at'}

                self.hedge_mode = False
            else:
                print('inverse futures')
                self.market_type = 'inverse_futures'
                self.endpoints = {'position': '/futures/private/position/list',
                                  'open_orders': '/futures/private/order',
                                  'create_order': '/futures/private/order/create',
                                  'cancel_order': '/futures/private/order/cancel',
                                  'ticks': '/v2/public/trading-records',
                                  'websocket': 'wss://stream.bybit.com/realtime',
                                  'created_at_key': 'created_at'}

        self.endpoints['balance'] = '/v2/private/wallet/balance'

    def determine_pos_side(self, o: dict) -> str:
        side = o['side'].lower()
        if side == 'buy':
            if 'entry' in o['order_link_id']:
                position_side = 'long'
            elif 'close' in o['order_link_id']:
                position_side = 'shrt'
            else:
                position_side = 'unknown'
        else:
            if 'entry' in o['order_link_id']:
                position_side = 'shrt'
            elif 'close' in o['order_link_id']:
                position_side = 'long'
            else:
                position_side = 'both'
        return position_side

    async def _init(self):
        info = await self.public_get('/v2/public/symbols')
        for e in info['result']:
            if e['name'] == self.symbol:
                break
        else:
            raise Exception('symbol missing')
        self.max_leverage = e['leverage_filter']['max_leverage']
        self.coin = e['base_currency']
        self.quot = e['quote_currency']
        self.price_step = self.config['price_step'] = float(e['price_filter']['tick_size'])
        self.qty_step = self.config['qty_step'] = float(e['lot_size_filter']['qty_step'])
        self.min_qty = self.config['min_qty'] = float(e['lot_size_filter']['min_trading_qty'])
        self.min_cost = self.config['min_cost'] = 0.0
        self.init_market_type()
        await super()._init()
        await self.init_order_book()
        await self.update_position()

    async def init_order_book(self):
        ticker = await self.private_get('/v2/public/tickers', {'symbol': self.symbol})
        self.ob = [float(ticker['result'][0]['bid_price']), float(ticker['result'][0]['ask_price'])]
        self.price = float(ticker['result'][0]['last_price'])

    async def fetch_open_orders(self) -> [dict]:
        fetched = await self.private_get(self.endpoints['open_orders'], {'symbol': self.symbol})

        return [{'order_id': elm['order_id'],
                 'custom_id': elm['order_link_id'],
                 'symbol': elm['symbol'],
                 'price': float(elm['price']),
                 'qty': float(elm['qty']),
                 'side': elm['side'].lower(),
                 'position_side': self.determine_pos_side(elm),
                 'timestamp': date_to_ts(elm[self.endpoints['created_at_key']])}
                for elm in fetched['result']]

    async def public_get(self, url: str, params: dict = {}) -> dict:
        async with self.session.get(self.base_endpoint + url, params=params) as response:
            result = await response.text()
        return json.loads(result)

    async def private_(self, type_: str, url: str, params: dict = {}) -> dict:
        timestamp = int(time() * 1000)
        params.update({'api_key': self.key, 'timestamp': timestamp})
        for k in params:
            if type(params[k]) == bool:
                params[k] = 'true' if params[k] else 'false'
            elif type(params[k]) == float:
                params[k] = str(params[k])
        params['sign'] = hmac.new(self.secret.encode('utf-8'),
                                  urlencode(sort_dict_keys(params)).encode('utf-8'),
                                  hashlib.sha256).hexdigest()
        async with getattr(self.session, type_)(self.base_endpoint + url, params=params) as response:
            result = await response.text()
        return json.loads(result)

    async def private_get(self, url: str, params: dict = {}) -> dict:
        return await self.private_('get', url, params)

    async def private_post(self, url: str, params: dict = {}) -> dict:
        return await self.private_('post', url, params)

    async def fetch_position(self) -> dict:
        position = {}
        if self.market_type == 'linear_perpetual':
            fetched, bal = await asyncio.gather(
                self.private_get(self.endpoints['position'], {'symbol': self.symbol}),
                self.private_get(self.endpoints['balance'], {'coin': self.quot})
            )
            long_pos = [e for e in fetched['result'] if e['side'] == 'Buy'][0]
            shrt_pos = [e for e in fetched['result'] if e['side'] == 'Sell'][0]
            position['wallet_balance'] = float(bal['result'][self.quot]['wallet_balance'])
        else:
            fetched, bal = await asyncio.gather(
                self.private_get(self.endpoints['position'], {'symbol': self.symbol}),
                self.private_get(self.endpoints['balance'], {'coin': self.coin})
            )
            position['wallet_balance'] = float(bal['result'][self.coin]['wallet_balance'])
            if self.market_type == 'inverse_perpetual':
                if fetched['result']['side'] == 'Buy':
                    long_pos = fetched['result']
                    shrt_pos = {'size': 0.0, 'entry_price': 0.0, 'leverage': 0.0, 'liq_price': 0.0}
                else:
                    long_pos = {'size': 0.0, 'entry_price': 0.0, 'leverage': 0.0, 'liq_price': 0.0}
                    shrt_pos = fetched['result']
            elif self.market_type == 'inverse_futures':
                long_pos = [e['data'] for e in fetched['result'] if e['data']['position_idx'] == 1][0]
                shrt_pos = [e['data'] for e in fetched['result'] if e['data']['position_idx'] == 2][0]

        position['long'] = {'size': float(long_pos['size']),
                            'price': float(long_pos['entry_price']),
                            'leverage': float(long_pos['leverage']),
                            'liquidation_price': float(long_pos['liq_price'])}
        position['shrt'] = {'size': -float(shrt_pos['size']),
                            'price': float(shrt_pos['entry_price']),
                            'leverage': float(shrt_pos['leverage']),
                            'liquidation_price': float(shrt_pos['liq_price'])}
        position['long']['upnl'] = calc_long_pnl(position['long']['price'], self.price,
                                                 position['long']['size'], self.xk['inverse'],
                                                 self.xk['contract_multiplier']) \
            if position['long']['price'] != 0.0 else 0.0
        position['shrt']['upnl'] = calc_shrt_pnl(position['shrt']['price'], self.price,
                                                 position['shrt']['size'], self.xk['inverse'],
                                                 self.xk['contract_multiplier']) \
            if position['shrt']['price'] != 0.0 else 0.0
        upnl = position['long']['upnl'] + position['shrt']['upnl']
        position['equity'] = position['wallet_balance'] + upnl
        return position

    async def execute_order(self, order: dict) -> dict:
        params = {'symbol': self.symbol,
                  'side': first_capitalized(order['side']),
                  'order_type': first_capitalized(order['type']),
                  'qty': float(order['qty']) if self.market_type == 'linear_perpetual' else int(order['qty']),
                  'close_on_trigger': False}
        if self.hedge_mode:
            params['position_idx'] = 1 if order['position_side'] == 'long' else 2
            if self.market_type == 'linear_perpetual':
                params['reduce_only'] = order['custom_id'] == 'close'
        else:
            params['position_idx'] = 0
            params['reduce_only'] = order['custom_id'] == 'close'
        if params['order_type'] == 'Limit':
            params['time_in_force'] = 'PostOnly'
            params['price'] = str(order['price'])
        else:
            params['time_in_force'] = 'GoodTillCancel'
        params['order_link_id'] = \
            f"{order['custom_id']}_{str(int(time() * 1000))[8:]}_{int(np.random.random() * 1000)}"
        o = await self.private_post(self.endpoints['create_order'], params)
        if o['result']:
            return {'symbol': o['result']['symbol'],
                    'side': o['result']['side'].lower(),
                    'position_side': order['position_side'],
                    'type': o['result']['order_type'].lower(),
                    'qty': o['result']['qty'],
                    'price': o['result']['price']}
        else:
            return o, order

    async def execute_cancellation(self, order: dict) -> [dict]:
        o = await self.private_post(self.endpoints['cancel_order'],
                                    {'symbol': self.symbol, 'order_id': order['order_id']})
        return {'symbol': self.symbol, 'side': order['side'],
                'position_side': order['position_side'],
                'qty': order['qty'], 'price': order['price']}

    async def fetch_ticks(self, from_id: int = None, do_print: bool = True):
        params = {'symbol': self.symbol, 'limit': 1000}
        if from_id is not None:
            params['from'] = max(0, from_id)
        try:
            ticks = await self.public_get(self.endpoints['ticks'], params)
        except Exception as e:
            print('error fetching ticks', e)
            return []
        try:
            trades = list(map(format_tick, ticks['result']))
            if do_print:
                print_(['fetched trades', self.symbol, trades[0]['trade_id'],
                        ts_to_date(float(trades[0]['timestamp']) / 1000)])
        except:
            trades = []
            if do_print:
                print_(['fetched no new trades', self.symbol])
        return trades

    def calc_margin_cost(self, qty: float, price: float) -> float:
        return qty / price / self.leverage

    def calc_max_pos_size(self, balance: float, price: float):
        return balance * price * self.leverage * 0.95

    async def init_exchange_config(self):
        try:
            # set cross mode
            if self.market_type == 'inverse_futures':
                res = await asyncio.gather(
                    self.private_post('/futures/private/position/leverage/save',
                                      {'symbol': self.symbol, 'position_idx': 1,
                                       'buy_leverage': 0, 'sell_leverage': 0}),
                    self.private_post('/futures/private/position/leverage/save',
                                      {'symbol': self.symbol, 'position_idx': 2,
                                       'buy_leverage': 0, 'sell_leverage': 0})
                )
                print(res)
                res = await self.private_post('/futures/private/position/switch-mode',
                                              {'symbol': self.symbol, 'mode': 3})
                print(res)
            elif self.market_type == 'linear_perpetual':
                res = await self.private_post('/private/linear/position/switch-isolated',
                                              {'symbol': self.symbol, 'is_isolated': False,
                                               'buy_leverage': 0,
                                               'sell_leverage': 0})
            elif self.market_type == 'inverse_perpetual':
                res = await self.private_post('/v2/private/position/leverage/save',
                                              {'symbol': self.symbol, 'leverage': 0})

            print(res)
        except Exception as e:
            print(e)

    def standardize_websocket_ticks(self, data: dict) -> [dict]:
        ticks = []
        for e in data['data']:
            try:
                ticks.append({'price': float(e['price']), 'qty': float(e['size']), 'is_buyer_maker': e['side'] == 'Sell'})
            except Exception as ex:
                print('error in websocket tick', e, ex)
        return ticks

    async def subscribe_ws(self, ws):
        params = {'op': 'subscribe', 'args': ['trade.' + self.symbol]}
        await ws.send(json.dumps(params))

    async def transfer(self, type_: str, amount: float, asset: str = 'USDT'):
        return {'code': '-1', 'msg': 'Transferring funds not supported for Bybit'}
