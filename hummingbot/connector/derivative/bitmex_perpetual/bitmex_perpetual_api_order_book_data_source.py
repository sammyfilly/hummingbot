import asyncio
import copy
import json
import logging
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from bidict import ValueDuplicationError, bidict

import hummingbot.connector.derivative.bitmex_perpetual.bitmex_perpetual_utils as utils
import hummingbot.connector.derivative.bitmex_perpetual.bitmex_perpetual_web_utils as web_utils
import hummingbot.connector.derivative.bitmex_perpetual.constants as CONSTANTS
from hummingbot.connector.derivative.bitmex_perpetual.bitmex_perpetual_order_book import BitmexPerpetualOrderBook
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.funding_info import FundingInfo
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant
from hummingbot.logger import HummingbotLogger


class BitmexPerpetualAPIOrderBookDataSource(OrderBookTrackerDataSource):

    _bpobds_logger: Optional[HummingbotLogger] = None
    _trading_pair_symbol_map: Dict[str, Mapping[str, str]] = {}
    _mapping_initialization_lock = asyncio.Lock()

    def __init__(
        self,
        trading_pairs: List[str] = None,
        domain: str = CONSTANTS.DOMAIN,
        throttler: Optional[AsyncThrottler] = None,
        api_factory: Optional[WebAssistantsFactory] = None,
    ):
        super().__init__(trading_pairs)
        self._api_factory: WebAssistantsFactory = WebAssistantsFactory(throttler)
        self._api_factory: WebAssistantsFactory = api_factory or web_utils.build_api_factory()
        self._ws_assistant: Optional[WSAssistant] = None
        self._order_book_create_function = lambda: OrderBook()
        self._domain = domain
        self._throttler = throttler or self._get_throttler_instance()
        self._funding_info: Dict[str, FundingInfo] = {}

        self._message_queue: Dict[int, asyncio.Queue] = defaultdict(asyncio.Queue)

    @property
    def funding_info(self) -> Dict[str, FundingInfo]:
        return copy.deepcopy(self._funding_info)

    def is_funding_info_initialized(self) -> bool:
        return all(trading_pair in self._funding_info for trading_pair in self._trading_pairs)

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._bpobds_logger is None:
            cls._bpobds_logger = logging.getLogger(__name__)
        return cls._bpobds_logger

    async def _get_ws_assistant(self) -> WSAssistant:
        if self._ws_assistant is None:
            self._ws_assistant = await self._api_factory.get_ws_assistant()
        return self._ws_assistant

    @classmethod
    def _get_throttler_instance(cls) -> AsyncThrottler:
        return AsyncThrottler(CONSTANTS.RATE_LIMITS)

    @classmethod
    async def get_last_traded_prices(cls,
                                     trading_pairs: List[str],
                                     domain: str = CONSTANTS.DOMAIN) -> Dict[str, float]:
        tasks = [cls.get_last_traded_price(t_pair, domain) for t_pair in trading_pairs]
        results = await safe_gather(*tasks)
        return dict(zip(trading_pairs, results))

    @classmethod
    async def get_last_traded_price(cls,
                                    trading_pair: str,
                                    domain: str = CONSTANTS.DOMAIN,
                                    api_factory: WebAssistantsFactory = None) -> float:
        api_factory = api_factory or web_utils.build_api_factory()

        throttler = cls._get_throttler_instance()

        params = {
            "symbol": await cls.convert_to_exchange_trading_pair(
                hb_trading_pair=trading_pair,
                domain=domain,
                throttler=throttler)}

        resp_json = await web_utils.api_request(
            CONSTANTS.EXCHANGE_INFO_URL,
            api_factory,
            throttler,
            domain,
            params=params
        )
        return float(resp_json[0]["lastPrice"])

    @classmethod
    def trading_pair_symbol_map_ready(cls, domain: str = CONSTANTS.DOMAIN):
        """
        Checks if the mapping from exchange symbols to client trading pairs has been initialized
        :param domain: the domain of the exchange being used
        :return: True if the mapping has been initialized, False otherwise
        """
        return domain in cls._trading_pair_symbol_map and len(cls._trading_pair_symbol_map[domain]) > 0

    @classmethod
    async def trading_pair_symbol_map(
            cls,
            domain: Optional[str] = CONSTANTS.DOMAIN,
            throttler: Optional[AsyncThrottler] = None,
            api_factory: WebAssistantsFactory = None
    ) -> Mapping[str, str]:
        if not cls.trading_pair_symbol_map_ready(domain=domain):
            api_factory = WebAssistantsFactory(throttler)
            async with cls._mapping_initialization_lock:
                # Check condition again (could have been initialized while waiting for the lock to be released)
                if not cls.trading_pair_symbol_map_ready(domain=domain):
                    await cls.init_trading_pair_symbols(domain, throttler, api_factory)

        return cls._trading_pair_symbol_map[domain]

    @classmethod
    async def init_trading_pair_symbols(
        cls,
        domain: str = CONSTANTS.DOMAIN,
        throttler: Optional[AsyncThrottler] = None,
        api_factory: WebAssistantsFactory = None
    ):
        """Initialize _trading_pair_symbol_map class variable"""
        mapping = bidict()

        api_factory = api_factory or web_utils.build_api_factory()

        throttler = throttler or cls._get_throttler_instance()
        params = {"filter": json.dumps({"typ": "FFWCSX"})}

        try:
            data = await web_utils.api_request(
                CONSTANTS.EXCHANGE_INFO_URL,
                api_factory,
                throttler,
                domain,
                params
            )
            for symbol_data in data:
                try:
                    mapping[symbol_data["symbol"]] = combine_to_hb_trading_pair(
                        symbol_data["rootSymbol"],
                        symbol_data["quoteCurrency"])
                except ValueDuplicationError:
                    continue

        except Exception as ex:
            cls.logger().error(f"There was an error requesting exchange info ({str(ex)})")

        cls._trading_pair_symbol_map[domain] = mapping

    @staticmethod
    async def fetch_trading_pairs(
        domain: str = CONSTANTS.DOMAIN,
        throttler: Optional[AsyncThrottler] = None,
        api_factory: WebAssistantsFactory = None
    ) -> List[str]:
        ob_source_cls = BitmexPerpetualAPIOrderBookDataSource
        trading_pair_list: List[str] = []
        symbols_map = await ob_source_cls.trading_pair_symbol_map(domain=domain, throttler=throttler, api_factory=api_factory)
        trading_pair_list.extend(list(symbols_map.values()))

        return trading_pair_list

    @classmethod
    async def convert_from_exchange_trading_pair(
            cls,
            exchange_trading_pair: str,
            domain: str = CONSTANTS.DOMAIN,
            throttler: Optional[AsyncThrottler] = None) -> str:

        symbol_map = await cls.trading_pair_symbol_map(
            domain=domain,
            throttler=throttler)
        try:
            pair = symbol_map[exchange_trading_pair]
        except KeyError:
            raise ValueError(f"There is no symbol mapping for exchange trading pair {exchange_trading_pair}")

        return pair

    @classmethod
    async def convert_to_exchange_trading_pair(
            cls,
            hb_trading_pair: str,
            domain = CONSTANTS.DOMAIN,
            throttler: Optional[AsyncThrottler] = None) -> str:

        symbol_map = await cls.trading_pair_symbol_map(
            domain=domain,
            throttler=throttler)
        try:
            symbol = symbol_map.inverse[hb_trading_pair]
        except KeyError:
            raise ValueError(f"There is no symbol mapping for trading pair {hb_trading_pair}")

        return symbol

    @staticmethod
    async def get_snapshot(
            trading_pair: str,
            limit: int = 1000,
            domain: str = CONSTANTS.DOMAIN,
            throttler: Optional[AsyncThrottler] = None,
            api_factory: WebAssistantsFactory = None
    ) -> Dict[str, Any]:
        ob_source_cls = BitmexPerpetualAPIOrderBookDataSource
        try:
            api_factory = api_factory or web_utils.build_api_factory()

            params = {"symbol": await ob_source_cls.convert_to_exchange_trading_pair(
                hb_trading_pair=trading_pair,
                domain=domain,
                throttler=throttler)}
            if limit != 0:
                params["limit"] = str(limit)

            throttler = throttler or ob_source_cls._get_throttler_instance()
            return await web_utils.api_request(
                CONSTANTS.SNAPSHOT_REST_URL, api_factory, throttler, domain, params
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise

    async def get_new_order_book(self, trading_pair: str) -> OrderBook:
        snapshot: List[Dict[str, Any]] = await self.get_snapshot(
            trading_pair,
            1000,
            self._domain,
            self._throttler,
            self._api_factory
        )
        bids = []
        asks = []
        size_currency = await utils.get_trading_pair_size_currency(
            await self.convert_to_exchange_trading_pair(
                hb_trading_pair=trading_pair,
                domain=self._domain,
                throttler=self._throttler
            )
        )
        for order in snapshot:
            if size_currency.is_base:
                order_details = [order['price'], order['size']]
            else:
                order_details = [order['price'], order['size'] / order['price']]
            asks.append(order_details) if order['side'] == "Sell" else bids.append(order_details)
        snapshot_dict = {
            "bids": bids,
            "asks": asks,
            "update_id": snapshot[-1]["id"]
        }

        snapshot_timestamp: float = time.time()
        snapshot_msg: OrderBookMessage = BitmexPerpetualOrderBook.snapshot_message_from_exchange(
            snapshot_dict, snapshot_timestamp, metadata={"trading_pair": trading_pair}
        )
        order_book = self.order_book_create_function()
        order_book.apply_snapshot(snapshot_msg.bids, snapshot_msg.asks, snapshot_msg.update_id)
        return order_book

    async def _get_funding_info_from_exchange(self, trading_pair: str) -> FundingInfo:
        """
        Fetches the funding information of the given trading pair from the exchange REST API. Parses and returns the
        respsonse as a FundingInfo data object.

        :param trading_pair: Trading pair of which its Funding Info is to be fetched
        :type trading_pair: str
        :return: Funding Information of the given trading pair
        :rtype: FundingInfo
        """
        api_factory = web_utils.build_api_factory()

        params = {"symbol": await self.convert_to_exchange_trading_pair(
            hb_trading_pair=trading_pair,
            domain=self._domain,
            throttler=self._throttler)}

        path = CONSTANTS.EXCHANGE_INFO_URL
        data = await web_utils.api_request(
            path,
            api_factory,
            self._throttler,
            self._domain,
            params
        )
        data = data[0]
        return FundingInfo(
            trading_pair=trading_pair,
            index_price=Decimal(data["lastPrice"]),
            mark_price=Decimal(data["fairPrice"]),
            next_funding_utc_timestamp=datetime.timestamp(
                datetime.strptime(
                    data["fundingTimestamp"], "%Y-%m-%dT%H:%M:%S.000Z"
                )
            ),
            rate=Decimal(data["fundingRate"]),
        )

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        """
        Returns the FundingInfo of the specified trading pair. If it does not exist, it will query the REST API.
        """
        if trading_pair not in self._funding_info:
            self._funding_info[trading_pair] = await self._get_funding_info_from_exchange(trading_pair)
        return self._funding_info[trading_pair]

    async def _subscribe_to_order_book_streams(self) -> WSAssistant:
        url = web_utils.wss_url("", self._domain)
        ws: WSAssistant = await self._get_ws_assistant()
        await ws.connect(ws_url=url)

        stream_channels = [
            "trade",
            "orderBookL2",
            "funding"
        ]
        for channel in stream_channels:
            params = []
            for trading_pair in self._trading_pairs:
                symbol = await self.convert_to_exchange_trading_pair(
                    hb_trading_pair=trading_pair,
                    domain=self._domain,
                    throttler=self._throttler)
                params.append(f"{channel}:{symbol}")
            payload = {
                "op": "subscribe",
                "args": params,
            }
            subscribe_request: WSJSONRequest = WSJSONRequest(
                payload=payload,
                is_auth_required=False
            )
            await ws.send(subscribe_request)

        return ws

    async def listen_for_subscriptions(self):
        ws = None
        while True:
            try:
                ws = await self._subscribe_to_order_book_streams()

                async for msg in ws.iter_messages():
                    if "result" in msg.data:
                        continue
                    if 'table' in msg.data:
                        if "orderBookL2" in msg.data["table"]:
                            self._message_queue[CONSTANTS.DIFF_STREAM_ID].put_nowait(msg)
                        elif "trade" in msg.data["table"]:
                            self._message_queue[CONSTANTS.TRADE_STREAM_ID].put_nowait(msg)
                        elif "funding" in msg.data["table"]:
                            self._message_queue[CONSTANTS.FUNDING_INFO_STREAM_ID].put_nowait(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(
                    "Unexpected error with Websocket connection. Retrying after 30 seconds...", exc_info=True
                )
                await self._sleep(30.0)
            finally:
                ws and await ws.disconnect()

    async def order_id_to_price(self, trading_pair: str, order_id: int):
        exchange_trading_pair = await self.convert_to_exchange_trading_pair(
            hb_trading_pair=trading_pair,
            domain=self._domain,
            throttler=self._throttler
        )
        id_tick = await utils.get_trading_pair_index_and_tick_size(exchange_trading_pair)
        trading_pair_id = id_tick.index
        instrument_tick_size = id_tick.tick_size
        return ((1e8 * trading_pair_id) - order_id) * instrument_tick_size

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            msg = await self._message_queue[CONSTANTS.DIFF_STREAM_ID].get()
            timestamp: float = time.time()
            if msg.data["action"] in ["update", "insert", "delete"]:
                msg.data["data_dict"] = {}
                exchange_trading_pair = msg.data["data"][0]["symbol"]
                trading_pair = await self.convert_from_exchange_trading_pair(
                    exchange_trading_pair=exchange_trading_pair,
                    domain=self._domain,
                    throttler=self._throttler)

                size_currency = await utils.get_trading_pair_size_currency(exchange_trading_pair)

                def size_fn(size, price):
                    final_size = size if size_currency.is_base else size / price
                    return final_size

                msg.data["data_dict"]["symbol"] = trading_pair
                asks = []
                bids = []
                for order in msg.data["data"]:
                    price = await self.order_id_to_price(trading_pair, order["id"])
                    amount = 0.0 if msg.data['action'] == "delete" else size_fn(order["size"], price)
                    order_details = [price, amount]
                    asks.append(order_details) if order["side"] == "Sell" else bids.append(order_details)

                msg.data["data_dict"]["bids"] = bids
                msg.data["data_dict"]["asks"] = asks
                order_book_message: OrderBookMessage = BitmexPerpetualOrderBook.diff_message_from_exchange(
                    msg.data, timestamp
                )
                output.put_nowait(order_book_message)

    async def listen_for_trades(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            msg = await self._message_queue[CONSTANTS.TRADE_STREAM_ID].get()
            msg.data["data_dict"] = {}
            trading_pair = await self.convert_from_exchange_trading_pair(
                exchange_trading_pair=msg.data["data"][0]["symbol"],
                domain=self._domain,
                throttler=self._throttler)
            for trade in msg.data["data"]:
                trade["symbol"] = trading_pair
                trade_message: OrderBookMessage = BitmexPerpetualOrderBook.trade_message_from_exchange(trade)
                output.put_nowait(trade_message)

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            try:
                for trading_pair in self._trading_pairs:
                    snapshot: Dict[str, Any] = await self.get_snapshot(
                        trading_pair, domain=self._domain, throttler=self._throttler, api_factory=self._api_factory
                    )

                    snapshot_timestamp: float = time.time()
                    bids = []
                    asks = []
                    size_currency = await utils.get_trading_pair_size_currency(
                        await self.convert_to_exchange_trading_pair(
                            hb_trading_pair=trading_pair,
                            domain=self._domain,
                            throttler=self._throttler
                        )
                    )
                    for order in snapshot:
                        if size_currency.is_base:
                            order_details = [order['price'], order['size']]
                        else:
                            order_details = [order['price'], order['size'] / order['price']]
                        asks.append(order_details) if order['side'] == "Sell" else bids.append(order_details)
                    snapshot_dict = {
                        "bids": bids,
                        "asks": asks,
                        "update_id": snapshot[-1]["id"]
                    }
                    snapshot_msg: OrderBookMessage = BitmexPerpetualOrderBook.snapshot_message_from_exchange(
                        snapshot_dict, snapshot_timestamp, metadata={"trading_pair": trading_pair}
                    )
                    output.put_nowait(snapshot_msg)
                    self.logger().debug(f"Saved order book snapshot for {trading_pair}")
                delta = CONSTANTS.ONE_HOUR - time.time() % CONSTANTS.ONE_HOUR
                await self._sleep(delta)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(
                    "Unexpected error occurred fetching orderbook snapshots. Retrying in 5 seconds...", exc_info=True
                )
                await self._sleep(5.0)
