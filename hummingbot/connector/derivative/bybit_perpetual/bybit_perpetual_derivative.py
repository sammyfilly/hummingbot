import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import pandas as pd
from bidict import bidict

import hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_constants as CONSTANTS
import hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_utils as bybit_utils
from hummingbot.connector.derivative.bybit_perpetual import bybit_perpetual_web_utils as web_utils
from hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_api_order_book_data_source import (
    BybitPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_auth import BybitPerpetualAuth
from hummingbot.connector.derivative.bybit_perpetual.bybit_perpetual_user_stream_data_source import (
    BybitPerpetualUserStreamDataSource,
)
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter

s_decimal_NaN = Decimal("nan")
s_decimal_0 = Decimal(0)


class BybitPerpetualDerivative(PerpetualDerivativePyBase):

    web_utils = web_utils

    def __init__(
        self,
        client_config_map: "ClientConfigAdapter",
        bybit_perpetual_api_key: str = None,
        bybit_perpetual_secret_key: str = None,
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):

        self.bybit_perpetual_api_key = bybit_perpetual_api_key
        self.bybit_perpetual_secret_key = bybit_perpetual_secret_key
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._domain = domain
        self._last_trade_history_timestamp = None

        super().__init__(client_config_map)

    @property
    def name(self) -> str:
        return CONSTANTS.EXCHANGE_NAME

    @property
    def authenticator(self) -> BybitPerpetualAuth:
        return BybitPerpetualAuth(self.bybit_perpetual_api_key, self.bybit_perpetual_secret_key)

    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        return web_utils.build_rate_limits(self.trading_pairs)

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return CONSTANTS.MAX_ID_LEN

    @property
    def client_order_id_prefix(self) -> str:
        return CONSTANTS.HBOT_BROKER_ID

    @property
    def trading_rules_request_path(self) -> str:
        return CONSTANTS.QUERY_SYMBOL_ENDPOINT

    @property
    def trading_pairs_request_path(self) -> str:
        return CONSTANTS.QUERY_SYMBOL_ENDPOINT

    @property
    def check_network_request_path(self) -> str:
        return CONSTANTS.SERVER_TIME_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return False

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def funding_fee_poll_interval(self) -> int:
        return 120

    def supported_order_types(self) -> List[OrderType]:
        """
        :return a list of OrderType supported by this connector
        """
        return [OrderType.LIMIT, OrderType.MARKET]

    def supported_position_modes(self) -> List[PositionMode]:
        if all(bybit_utils.is_linear_perpetual(tp) for tp in self._trading_pairs):
            return [PositionMode.ONEWAY, PositionMode.HEDGE]
        elif all(not bybit_utils.is_linear_perpetual(tp) for tp in self._trading_pairs):
            # As of ByBit API v2, we only support ONEWAY mode for non-linear perpetuals
            return [PositionMode.ONEWAY]
        else:
            self.logger().warning(
                "Currently there is no support for both linear and non-linear markets concurrently."
                " Please start another hummingbot instance."
            )
            return []

    def get_buy_collateral_token(self, trading_pair: str) -> str:
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.buy_order_collateral_token

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        trading_rule: TradingRule = self._trading_rules[trading_pair]
        return trading_rule.sell_order_collateral_token

    def start(self, clock: Clock, timestamp: float):
        super().start(clock, timestamp)
        if self._domain == CONSTANTS.DEFAULT_DOMAIN and self.is_trading_required:
            self.set_position_mode(PositionMode.HEDGE)

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        error_description = str(request_exception)
        ts_error_target_str = self._format_ret_code_for_print(ret_code=CONSTANTS.RET_CODE_AUTH_TIMESTAMP_ERROR)
        param_error_target_str = (
            f"{self._format_ret_code_for_print(ret_code=CONSTANTS.RET_CODE_PARAMS_ERROR)} - invalid timestamp"
        )
        return (
            ts_error_target_str in error_description
            or param_error_target_str in error_description
        )

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        # TODO: implement this method correctly for the connector
        # The default implementation was added when the functionality to detect not found orders was introduced in the
        # ExchangePyBase class. Also fix the unit test test_lost_order_removed_if_not_found_during_order_status_update
        # when replacing the dummy implementation
        return False

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        # TODO: implement this method correctly for the connector
        # The default implementation was added when the functionality to detect not found orders was introduced in the
        # ExchangePyBase class. Also fix the unit test test_cancel_order_not_found_in_the_exchange when replacing the
        # dummy implementation
        return False

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        data = {"symbol": await self.exchange_symbol_associated_to_pair(tracked_order.trading_pair)}
        if tracked_order.exchange_order_id:
            data["order_id"] = tracked_order.exchange_order_id
        else:
            data["order_link_id"] = tracked_order.client_order_id
        cancel_result = await self._api_post(
            path_url=CONSTANTS.CANCEL_ACTIVE_ORDER_PATH_URL,
            data=data,
            is_auth_required=True,
            trading_pair=tracked_order.trading_pair,
        )
        response_code = cancel_result["ret_code"]

        if response_code != CONSTANTS.RET_CODE_OK:
            if response_code == CONSTANTS.RET_CODE_ORDER_NOT_EXISTS:
                await self._order_tracker.process_order_not_found(order_id)
            formatted_ret_code = self._format_ret_code_for_print(response_code)
            raise IOError(f"{formatted_ret_code} - {cancel_result['ret_msg']}")

        return True

    async def _place_order(
        self,
        order_id: str,
        trading_pair: str,
        amount: Decimal,
        trade_type: TradeType,
        order_type: OrderType,
        price: Decimal,
        position_action: PositionAction = PositionAction.NIL,
        **kwargs,
    ) -> Tuple[str, float]:
        position_idx = self._get_position_idx(trade_type, position_action)
        data = {
            "side": "Buy" if trade_type == TradeType.BUY else "Sell",
            "symbol": await self.exchange_symbol_associated_to_pair(trading_pair),
            "qty": float(amount),
            "time_in_force": CONSTANTS.DEFAULT_TIME_IN_FORCE,
            "close_on_trigger": position_action == PositionAction.CLOSE,
            "order_link_id": order_id,
            "reduce_only": position_action == PositionAction.CLOSE,
            "position_idx": position_idx,
            "order_type": CONSTANTS.ORDER_TYPE_MAP[order_type],
        }
        if order_type.is_limit_type():
            data["price"] = float(price)

        resp = await self._api_post(
            path_url=CONSTANTS.PLACE_ACTIVE_ORDER_PATH_URL,
            data=data,
            is_auth_required=True,
            trading_pair=trading_pair,
            headers={"referer": CONSTANTS.HBOT_BROKER_ID},
            **kwargs,
        )

        if resp["ret_code"] != CONSTANTS.RET_CODE_OK:
            formatted_ret_code = self._format_ret_code_for_print(resp['ret_code'])
            raise IOError(f"Error submitting order {order_id}: {formatted_ret_code} - {resp['ret_msg']}")

        return str(resp["result"]["order_id"]), self.current_timestamp

    def _get_position_idx(self, trade_type: TradeType, position_action: PositionAction) -> int:
        if position_action == PositionAction.NIL:
            raise NotImplementedError
        if self.position_mode == PositionMode.ONEWAY:
            position_idx = CONSTANTS.POSITION_IDX_ONEWAY
        elif trade_type == TradeType.BUY:
            if position_action == PositionAction.CLOSE:
                position_idx = CONSTANTS.POSITION_IDX_HEDGE_SELL
            else:  # position_action == PositionAction.Open
                position_idx = CONSTANTS.POSITION_IDX_HEDGE_BUY
        elif trade_type == TradeType.SELL:
            if position_action == PositionAction.CLOSE:
                position_idx = CONSTANTS.POSITION_IDX_HEDGE_BUY
            else:  # position_action == PositionAction.Open
                position_idx = CONSTANTS.POSITION_IDX_HEDGE_SELL
        else:  # trade_type == TradeType.RANGE
            raise NotImplementedError

        return position_idx

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = is_maker or False
        return build_trade_fee(
            self.name,
            is_maker,
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            amount=amount,
            price=price,
        )

    async def _update_trading_fees(self):
        pass

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            auth=self._auth,
        )

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return BybitPerpetualAPIOrderBookDataSource(
            self.trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self._domain,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return BybitPerpetualUserStreamDataSource(
            auth=self._auth,
            api_factory=self._web_assistants_factory,
            domain=self._domain,
        )

    async def _status_polling_loop_fetch_updates(self):
        await safe_gather(
            self._update_trade_history(),
            self._update_order_status(),
            self._update_balances(),
            self._update_positions(),
        )

    async def _update_trade_history(self):
        """
        Calls REST API to get trade history (order fills)
        """

        trade_history_tasks = []

        for trading_pair in self._trading_pairs:
            exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
            body_params = {
                "symbol": exchange_symbol,
                "limit": 200,
            }
            if self._last_trade_history_timestamp:
                body_params["start_time"] = int(int(self._last_trade_history_timestamp) * 1e3)

            trade_history_tasks.append(
                asyncio.create_task(self._api_get(
                    path_url=CONSTANTS.USER_TRADE_RECORDS_PATH_URL,
                    params=body_params,
                    is_auth_required=True,
                    trading_pair=trading_pair,
                ))
            )

        raw_responses: List[Dict[str, Any]] = await safe_gather(*trade_history_tasks, return_exceptions=True)

        # Initial parsing of responses. Joining all the responses
        parsed_history_resps: List[Dict[str, Any]] = []
        for trading_pair, resp in zip(self._trading_pairs, raw_responses):
            if not isinstance(resp, Exception):
                self._last_trade_history_timestamp = float(resp["time_now"])
                trade_entries = (resp["result"]["trade_list"]
                                 if "trade_list" in resp["result"]
                                 else resp["result"]["data"])
                if trade_entries:
                    parsed_history_resps.extend(trade_entries)
            else:
                self.logger().network(
                    f"Error fetching status update for {trading_pair}: {resp}.",
                    app_warning_msg=f"Failed to fetch status update for {trading_pair}."
                )

        # Trade updates must be handled before any order status updates.
        for trade in parsed_history_resps:
            self._process_trade_event_message(trade)

    async def _update_order_status(self):
        """
        Calls REST API to get order status
        """

        active_orders: List[InFlightOrder] = list(self.in_flight_orders.values())

        tasks = [
            asyncio.create_task(
                self._request_order_status_data(tracked_order=active_order)
            )
            for active_order in active_orders
        ]
        raw_responses: List[Dict[str, Any]] = await safe_gather(*tasks, return_exceptions=True)

        # Initial parsing of responses. Removes Exceptions.
        parsed_status_responses: List[Dict[str, Any]] = []
        for resp, active_order in zip(raw_responses, active_orders):
            if not isinstance(resp, Exception):
                parsed_status_responses.append(resp["result"])
            else:
                self.logger().network(
                    f"Error fetching status update for the order {active_order.client_order_id}: {resp}.",
                    app_warning_msg=f"Failed to fetch status update for the order {active_order.client_order_id}."
                )
                await self._order_tracker.process_order_not_found(active_order.client_order_id)

        for order_status in parsed_status_responses:
            self._process_order_event_message(order_status)

    async def _update_balances(self):
        """
        Calls REST API to update total and available balances
        """
        wallet_balance: Dict[str, Dict[str, Any]] = await self._api_get(
            path_url=CONSTANTS.GET_WALLET_BALANCE_PATH_URL,
            is_auth_required=True,
        )

        if wallet_balance["ret_code"] != CONSTANTS.RET_CODE_OK:
            formatted_ret_code = self._format_ret_code_for_print(wallet_balance['ret_code'])
            raise IOError(f"{formatted_ret_code} - {wallet_balance['ret_msg']}")

        self._account_available_balances.clear()
        self._account_balances.clear()

        if wallet_balance["result"] is not None:
            for asset_name, balance_json in wallet_balance["result"].items():
                self._account_balances[asset_name] = Decimal(str(balance_json["wallet_balance"]))
                self._account_available_balances[asset_name] = Decimal(str(balance_json["available_balance"]))

    async def _update_positions(self):
        """
        Retrieves all positions using the REST API.
        """
        position_tasks = []

        for trading_pair in self._trading_pairs:
            ex_trading_pair = await self.exchange_symbol_associated_to_pair(trading_pair)
            body_params = {"symbol": ex_trading_pair}
            position_tasks.append(
                asyncio.create_task(self._api_get(
                    path_url=CONSTANTS.GET_POSITIONS_PATH_URL,
                    params=body_params,
                    is_auth_required=True,
                    trading_pair=trading_pair,
                ))
            )

        raw_responses: List[Dict[str, Any]] = await safe_gather(*position_tasks, return_exceptions=True)

        # Initial parsing of responses. Joining all the responses
        parsed_resps: List[Dict[str, Any]] = []
        for resp, trading_pair in zip(raw_responses, self._trading_pairs):
            if isinstance(resp, Exception):
                self.logger().error(f"Error fetching positions for {trading_pair}. Response: {resp}")

            elif result := resp["result"]:
                position_entries = result if isinstance(result, list) else [result]
                parsed_resps.extend(position_entries)
        for position in parsed_resps:
            data = position
            ex_trading_pair = data.get("symbol")
            hb_trading_pair = await self.trading_pair_associated_to_exchange_symbol(ex_trading_pair)
            position_side = PositionSide.LONG if data["side"] == "Buy" else PositionSide.SHORT
            unrealized_pnl = Decimal(str(data["unrealised_pnl"]))
            entry_price = Decimal(str(data["entry_price"]))
            amount = Decimal(str(data["size"]))
            leverage = Decimal(str(data["leverage"])) if bybit_utils.is_linear_perpetual(hb_trading_pair) \
                    else Decimal(str(data["effective_leverage"]))
            pos_key = self._perpetual_trading.position_key(hb_trading_pair, position_side)
            if amount != s_decimal_0:
                position = Position(
                    trading_pair=hb_trading_pair,
                    position_side=position_side,
                    unrealized_pnl=unrealized_pnl,
                    entry_price=entry_price,
                    amount=amount * (Decimal("-1.0") if position_side == PositionSide.SHORT else Decimal("1.0")),
                    leverage=leverage,
                )
                self._perpetual_trading.set_position(pos_key, position)
            else:
                self._perpetual_trading.remove_position(pos_key)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []

        if order.exchange_order_id is not None:
            try:
                all_fills_response = await self._request_order_fills(order=order)
                trades_list_key = "data" if bybit_utils.is_linear_perpetual(order.trading_pair) else "trade_list"
                fills_data = all_fills_response["result"].get(trades_list_key, [])

                if fills_data is not None:
                    for fill_data in fills_data:
                        trade_update = self._parse_trade_update(trade_msg=fill_data, tracked_order=order)
                        trade_updates.append(trade_update)
            except IOError as ex:
                if not self._is_request_exception_related_to_time_synchronizer(request_exception=ex):
                    raise

        return trade_updates

    async def _request_order_fills(self, order: InFlightOrder) -> Dict[str, Any]:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair=order.trading_pair)
        body_params = {
            "order_id": order.exchange_order_id,
            "symbol": exchange_symbol,
        }
        return await self._api_get(
            path_url=CONSTANTS.USER_TRADE_RECORDS_PATH_URL,
            params=body_params,
            is_auth_required=True,
            trading_pair=order.trading_pair,
        )

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        try:
            order_status_data = await self._request_order_status_data(tracked_order=tracked_order)
            order_msg = order_status_data["result"]
            client_order_id = str(order_msg["order_link_id"])

            order_update: OrderUpdate = OrderUpdate(
                trading_pair=tracked_order.trading_pair,
                update_timestamp=self.current_timestamp,
                new_state=CONSTANTS.ORDER_STATE[order_msg["order_status"]],
                client_order_id=client_order_id,
                exchange_order_id=order_msg["order_id"],
            )

            return order_update

        except IOError as ex:
            if self._is_request_exception_related_to_time_synchronizer(request_exception=ex):
                order_update = OrderUpdate(
                    client_order_id=tracked_order.client_order_id,
                    trading_pair=tracked_order.trading_pair,
                    update_timestamp=self.current_timestamp,
                    new_state=tracked_order.current_state,
                )
            else:
                raise

        return order_update

    async def _request_order_status_data(self, tracked_order: InFlightOrder) -> Dict:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(tracked_order.trading_pair)
        query_params = {
            "symbol": exchange_symbol,
            "order_link_id": tracked_order.client_order_id
        }
        if tracked_order.exchange_order_id is not None:
            query_params["order_id"] = tracked_order.exchange_order_id

        return await self._api_get(
            path_url=CONSTANTS.QUERY_ACTIVE_ORDER_PATH_URL,
            params=query_params,
            is_auth_required=True,
            trading_pair=tracked_order.trading_pair,
        )

    async def _user_stream_event_listener(self):
        """
        Listens to message in _user_stream_tracker.user_stream queue.
        """
        async for event_message in self._iter_user_event_queue():
            try:
                endpoint = web_utils.endpoint_from_message(event_message)
                payload = web_utils.payload_from_message(event_message)

                if endpoint == CONSTANTS.WS_SUBSCRIPTION_POSITIONS_ENDPOINT_NAME:
                    for position_msg in payload:
                        await self._process_account_position_event(position_msg)
                elif endpoint == CONSTANTS.WS_SUBSCRIPTION_ORDERS_ENDPOINT_NAME:
                    for order_msg in payload:
                        self._process_order_event_message(order_msg)
                elif endpoint == CONSTANTS.WS_SUBSCRIPTION_EXECUTIONS_ENDPOINT_NAME:
                    for trade_msg in payload:
                        self._process_trade_event_message(trade_msg)
                elif endpoint == CONSTANTS.WS_SUBSCRIPTION_WALLET_ENDPOINT_NAME:
                    for wallet_msg in payload:
                        self._process_wallet_event_message(wallet_msg)
                elif endpoint is None:
                    self.logger().error(f"Could not extract endpoint from {event_message}.")
                    raise ValueError
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception("Unexpected error in user stream listener loop.")
                await self._sleep(5.0)

    async def _process_account_position_event(self, position_msg: Dict[str, Any]):
        """
        Updates position
        :param position_msg: The position event message payload
        """
        ex_trading_pair = position_msg["symbol"]
        trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=ex_trading_pair)
        position_side = PositionSide.LONG if position_msg["side"] == "Buy" else PositionSide.SHORT
        position_value = Decimal(str(position_msg["position_value"]))
        entry_price = Decimal(str(position_msg["entry_price"]))
        amount = Decimal(str(position_msg["size"]))
        leverage = Decimal(str(position_msg["leverage"]))
        unrealized_pnl = position_value - (amount * entry_price * leverage)
        pos_key = self._perpetual_trading.position_key(trading_pair, position_side)
        if amount != s_decimal_0:
            position = Position(
                trading_pair=trading_pair,
                position_side=position_side,
                unrealized_pnl=unrealized_pnl,
                entry_price=entry_price,
                amount=amount * (Decimal("-1.0") if position_side == PositionSide.SHORT else Decimal("1.0")),
                leverage=leverage,
            )
            self._perpetual_trading.set_position(pos_key, position)
        else:
            self._perpetual_trading.remove_position(pos_key)

        # Trigger balance update because Bybit doesn't have balance updates through the websocket
        safe_ensure_future(self._update_balances())

    def _process_trade_event_message(self, trade_msg: Dict[str, Any]):
        """
        Updates in-flight order and trigger order filled event for trade message received. Triggers order completed
        event if the total executed amount equals to the specified order amount.
        :param trade_msg: The trade event message payload
        """

        client_order_id = str(trade_msg["order_link_id"])
        fillable_order = self._order_tracker.all_fillable_orders.get(client_order_id)

        if fillable_order is not None:
            trade_update = self._parse_trade_update(trade_msg=trade_msg, tracked_order=fillable_order)
            self._order_tracker.process_trade_update(trade_update)

    def _parse_trade_update(self, trade_msg: Dict, tracked_order: InFlightOrder) -> TradeUpdate:
        trade_id: str = str(trade_msg["exec_id"])

        fee_asset = tracked_order.quote_asset
        fee_amount = Decimal(trade_msg["exec_fee"])
        position_side = trade_msg["side"]
        position_action = (PositionAction.OPEN
                           if (tracked_order.trade_type is TradeType.BUY and position_side == "Buy"
                               or tracked_order.trade_type is TradeType.SELL and position_side == "Sell")
                           else PositionAction.CLOSE)

        flat_fees = [] if fee_amount == Decimal("0") else [TokenAmount(amount=fee_amount, token=fee_asset)]

        fee = TradeFeeBase.new_perpetual_fee(
            fee_schema=self.trade_fee_schema(),
            position_action=position_action,
            percent_token=fee_asset,
            flat_fees=flat_fees,
        )

        exec_price = Decimal(trade_msg["exec_price"]) if "exec_price" in trade_msg else Decimal(trade_msg["price"])
        exec_time = (
            trade_msg["exec_time"]
            if "exec_time" in trade_msg
            else pd.Timestamp(trade_msg["trade_time"]).timestamp()
        )

        trade_update: TradeUpdate = TradeUpdate(
            trade_id=trade_id,
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=str(trade_msg["order_id"]),
            trading_pair=tracked_order.trading_pair,
            fill_timestamp=exec_time,
            fill_price=exec_price,
            fill_base_amount=Decimal(trade_msg["exec_qty"]),
            fill_quote_amount=exec_price * Decimal(trade_msg["exec_qty"]),
            fee=fee,
        )

        return trade_update

    def _process_order_event_message(self, order_msg: Dict[str, Any]):
        """
        Updates in-flight order and triggers cancellation or failure event if needed.
        :param order_msg: The order event message payload
        """
        order_status = CONSTANTS.ORDER_STATE[order_msg["order_status"]]
        client_order_id = str(order_msg["order_link_id"])
        updatable_order = self._order_tracker.all_updatable_orders.get(client_order_id)

        if updatable_order is not None:
            new_order_update: OrderUpdate = OrderUpdate(
                trading_pair=updatable_order.trading_pair,
                update_timestamp=self.current_timestamp,
                new_state=order_status,
                client_order_id=client_order_id,
                exchange_order_id=order_msg["order_id"],
            )
            self._order_tracker.process_order_update(new_order_update)

    def _process_wallet_event_message(self, wallet_msg: Dict[str, Any]):
        """
        Updates account balances.
        :param wallet_msg: The account balance update message payload
        """
        symbol = wallet_msg.get("coin", "USDT")
        self._account_balances[symbol] = Decimal(str(wallet_msg["wallet_balance"]))
        self._account_available_balances[symbol] = Decimal(str(wallet_msg["available_balance"]))

    async def _format_trading_rules(self, instrument_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Converts JSON API response into a local dictionary of trading rules.
        :param instrument_info_dict: The JSON API response.
        :returns: A dictionary of trading pair to its respective TradingRule.
        """
        trading_rules = {}
        symbol_map = await self.trading_pair_symbol_map()
        for instrument in instrument_info_dict["result"]:
            try:
                exchange_symbol = instrument["name"]
                if exchange_symbol in symbol_map:
                    trading_pair = combine_to_hb_trading_pair(instrument['base_currency'], instrument['quote_currency'])
                    is_linear = bybit_utils.is_linear_perpetual(trading_pair)
                    collateral_token = instrument["quote_currency"] if is_linear else instrument["base_currency"]
                    trading_rules[trading_pair] = TradingRule(
                        trading_pair=trading_pair,
                        min_order_size=Decimal(str(instrument["lot_size_filter"]["min_trading_qty"])),
                        max_order_size=Decimal(str(instrument["lot_size_filter"]["max_trading_qty"])),
                        min_price_increment=Decimal(str(instrument["price_filter"]["tick_size"])),
                        min_base_amount_increment=Decimal(str(instrument["lot_size_filter"]["qty_step"])),
                        buy_order_collateral_token=collateral_token,
                        sell_order_collateral_token=collateral_token,
                    )
            except Exception:
                self.logger().exception(f"Error parsing the trading pair rule: {instrument}. Skipping...")
        return list(trading_rules.values())

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        for symbol_data in filter(bybit_utils.is_exchange_information_valid, exchange_info["result"]):
            exchange_symbol = symbol_data["name"]
            base = symbol_data["base_currency"]
            quote = symbol_data["quote_currency"]
            trading_pair = combine_to_hb_trading_pair(base, quote)
            if trading_pair in mapping.inverse:
                self._resolve_trading_pair_symbols_duplicate(mapping, exchange_symbol, base, quote)
            else:
                mapping[exchange_symbol] = trading_pair
        self._set_trading_pair_symbol_map(mapping)

    def _resolve_trading_pair_symbols_duplicate(self, mapping: bidict, new_exchange_symbol: str, base: str, quote: str):
        """Resolves name conflicts provoked by futures contracts.

        If the expected BASEQUOTE combination matches one of the exchange symbols, it is the one taken, otherwise,
        the trading pair is removed from the map and an error is logged.
        """
        expected_exchange_symbol = f"{base}{quote}"
        trading_pair = combine_to_hb_trading_pair(base, quote)
        current_exchange_symbol = mapping.inverse[trading_pair]
        if current_exchange_symbol == expected_exchange_symbol:
            pass
        elif new_exchange_symbol == expected_exchange_symbol:
            mapping.pop(current_exchange_symbol)
            mapping[new_exchange_symbol] = trading_pair
        else:
            self.logger().error(f"Could not resolve the exchange symbols {new_exchange_symbol} and {current_exchange_symbol}")
            mapping.pop(current_exchange_symbol)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
        params = {"symbol": exchange_symbol}

        resp_json = await self._api_get(
            path_url=CONSTANTS.LATEST_SYMBOL_INFORMATION_ENDPOINT,
            params=params,
        )

        return float(resp_json["result"][0]["last_price"])

    async def _trading_pair_position_mode_set(self, mode: PositionMode, trading_pair: str) -> Tuple[bool, str]:
        msg = ""
        success = True

        api_mode = CONSTANTS.POSITION_MODE_MAP[mode]
        if is_linear := bybit_utils.is_linear_perpetual(trading_pair):
            exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
            data = {"symbol": exchange_symbol, "mode": api_mode}

            response = await self._api_post(
                path_url=CONSTANTS.SET_POSITION_MODE_URL,
                data=data,
                is_auth_required=True,
            )

            response_code = response["ret_code"]

            if response_code not in [CONSTANTS.RET_CODE_OK, CONSTANTS.RET_CODE_MODE_NOT_MODIFIED]:
                formatted_ret_code = self._format_ret_code_for_print(response_code)
                msg = f"{formatted_ret_code} - {response['ret_msg']}"
                success = False
        else:
            #  Inverse Perpetuals don't have set_position_mode()
            msg = "Inverse Perpetuals don't allow for a position mode change."
            success = False

        return success, msg

    async def _set_trading_pair_leverage(self, trading_pair: str, leverage: int) -> Tuple[bool, str]:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)

        if bybit_utils.is_linear_perpetual(trading_pair):
            data = {
                "symbol": exchange_symbol,
                "buy_leverage": leverage,
                "sell_leverage": leverage
            }
        else:
            data = {
                "symbol": exchange_symbol,
                "leverage": leverage
            }

        resp: Dict[str, Any] = await self._api_post(
            path_url=CONSTANTS.SET_LEVERAGE_PATH_URL,
            data=data,
            is_auth_required=True,
            trading_pair=trading_pair,
        )

        success = False
        msg = ""
        if resp["ret_code"] == CONSTANTS.RET_CODE_OK or (resp["ret_code"] == CONSTANTS.RET_CODE_LEVERAGE_NOT_MODIFIED and resp["ret_msg"] == "leverage not modified"):
            success = True
        else:
            formatted_ret_code = self._format_ret_code_for_print(resp['ret_code'])
            msg = f"{formatted_ret_code} - {resp['ret_msg']}"

        return success, msg

    async def _fetch_last_fee_payment(self, trading_pair: str) -> Tuple[int, Decimal, Decimal]:
        exchange_symbol = await self.exchange_symbol_associated_to_pair(trading_pair)

        params = {
            "symbol": exchange_symbol
        }
        raw_response: Dict[str, Any] = await self._api_get(
            path_url=CONSTANTS.GET_LAST_FUNDING_RATE_PATH_URL,
            params=params,
            is_auth_required=True,
            trading_pair=trading_pair,
        )
        if data := raw_response["result"]:
            funding_rate: Decimal = Decimal(str(data["funding_rate"]))
            position_size: Decimal = Decimal(str(data["size"]))
            payment: Decimal = funding_rate * position_size
            timestamp: int = (
                int(pd.Timestamp(data["exec_time"], tz="UTC").timestamp())
                if bybit_utils.is_linear_perpetual(trading_pair)
                else int(data["exec_timestamp"])
            )
        else:
            # An empty funding fee/payment is retrieved.
            timestamp, funding_rate, payment = 0, Decimal("-1"), Decimal("-1")
        return timestamp, funding_rate, payment

    async def _api_request(self,
                           path_url,
                           method: RESTMethod = RESTMethod.GET,
                           params: Optional[Dict[str, Any]] = None,
                           data: Optional[Dict[str, Any]] = None,
                           is_auth_required: bool = False,
                           return_err: bool = False,
                           limit_id: Optional[str] = None,
                           trading_pair: Optional[str] = None,
                           **kwargs) -> Dict[str, Any]:

        rest_assistant = await self._web_assistants_factory.get_rest_assistant()
        if limit_id is None:
            limit_id = web_utils.get_rest_api_limit_id_for_endpoint(
                endpoint=path_url,
                trading_pair=trading_pair,
            )
        url = web_utils.get_rest_url_for_endpoint(endpoint=path_url, trading_pair=trading_pair, domain=self._domain)

        return await rest_assistant.execute_request(
            url=url,
            params=params,
            data=data,
            method=method,
            is_auth_required=is_auth_required,
            return_err=return_err,
            throttler_limit_id=limit_id if limit_id else path_url,
        )

    @staticmethod
    def _format_ret_code_for_print(ret_code: Union[str, int]) -> str:
        return f"ret_code <{ret_code}>"
