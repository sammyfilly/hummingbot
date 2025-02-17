import re
from os import listdir
from os.path import exists, isfile, join
from typing import List

from prompt_toolkit.completion import CompleteEvent, Completer, WordCompleter
from prompt_toolkit.document import Document

from hummingbot.client.command.connect_command import OPTIONS as CONNECT_OPTIONS
from hummingbot.client.settings import (
    GATEWAY_CONNECTORS,
    PMM_SCRIPTS_PATH,
    SCRIPT_STRATEGIES_PATH,
    STRATEGIES,
    STRATEGIES_CONF_DIR_PATH,
    AllConnectorSettings,
)
from hummingbot.client.ui.parser import ThrowingArgumentParser
from hummingbot.core.rate_oracle.rate_oracle import RATE_ORACLE_SOURCES
from hummingbot.core.utils.gateway_config_utils import list_gateway_wallets
from hummingbot.core.utils.trading_pair_fetcher import TradingPairFetcher


def file_name_list(path, file_extension):
    if not exists(path):
        return []
    return sorted([f for f in listdir(path) if isfile(join(path, f)) and f.endswith(file_extension)])


class HummingbotCompleter(Completer):
    def __init__(self, hummingbot_application):
        super(HummingbotCompleter, self).__init__()
        self.hummingbot_application = hummingbot_application
        self._path_completer = WordCompleter(file_name_list(str(STRATEGIES_CONF_DIR_PATH), "yml"))
        self._command_completer = WordCompleter(self.parser.commands, ignore_case=True)
        self._exchange_completer = WordCompleter(sorted(AllConnectorSettings.get_connector_settings().keys()), ignore_case=True)
        self._spot_exchange_completer = WordCompleter(sorted(AllConnectorSettings.get_exchange_names()), ignore_case=True)
        self._exchange_amm_completer = WordCompleter(
            sorted(
                AllConnectorSettings.get_exchange_names().union(
                    AllConnectorSettings.get_gateway_amm_connector_names()
                ).union(
                    AllConnectorSettings.get_gateway_clob_connector_names()
                )
            ), ignore_case=True
        )
        self._exchange_clob_completer = WordCompleter(sorted(AllConnectorSettings.get_exchange_names().union(
            AllConnectorSettings.get_gateway_clob_connector_names())), ignore_case=True)
        self._evm_amm_lp_completer = WordCompleter(sorted(AllConnectorSettings.get_gateway_evm_amm_lp_connector_names()), ignore_case=True)
        self._trading_timeframe_completer = WordCompleter(["infinite", "from_date_to_date", "daily_between_times"], ignore_case=True)
        self._derivative_completer = WordCompleter(AllConnectorSettings.get_derivative_names(), ignore_case=True)
        self._derivative_exchange_completer = WordCompleter(AllConnectorSettings.get_derivative_names().difference(AllConnectorSettings.get_derivative_dex_names()), ignore_case=True)
        self._connect_option_completer = WordCompleter(CONNECT_OPTIONS, ignore_case=True)
        self._export_completer = WordCompleter(["keys", "trades"], ignore_case=True)
        self._balance_completer = WordCompleter(["limit", "paper"], ignore_case=True)
        self._history_completer = WordCompleter(["--days", "--verbose", "--precision"], ignore_case=True)
        self._gateway_completer = WordCompleter(["config", "connect", "connector-tokens", "generate-certs", "test-connection", "list", "approve-tokens"], ignore_case=True)
        self._gateway_connect_completer = WordCompleter(GATEWAY_CONNECTORS, ignore_case=True)
        self._gateway_connector_tokens_completer = WordCompleter(
            sorted(
                AllConnectorSettings.get_gateway_amm_connector_names().union(
                    AllConnectorSettings.get_gateway_clob_connector_names()
                )
            ), ignore_case=True
        )
        self._gateway_approve_tokens_completer = WordCompleter(
            sorted(
                AllConnectorSettings.get_gateway_amm_connector_names().union(
                    AllConnectorSettings.get_gateway_clob_connector_names()
                )
            ), ignore_case=True
        )
        self._gateway_config_completer = WordCompleter(hummingbot_application.gateway_config_keys, ignore_case=True)
        self._strategy_completer = WordCompleter(STRATEGIES, ignore_case=True)
        self._py_file_completer = WordCompleter(file_name_list(str(PMM_SCRIPTS_PATH), "py"))
        self._script_strategy_completer = WordCompleter(file_name_list(str(SCRIPT_STRATEGIES_PATH), "py"))
        self._rate_oracle_completer = WordCompleter(list(RATE_ORACLE_SOURCES.keys()), ignore_case=True)
        self._mqtt_completer = WordCompleter(["start", "stop", "restart"], ignore_case=True)
        self._gateway_chains = []
        self._gateway_networks = []
        self._list_gateway_wallets_parameters = {"wallets": [], "chain": ""}

    def set_gateway_chains(self, gateway_chains):
        self._gateway_chains = gateway_chains

    def set_gateway_networks(self, gateway_networks):
        self._gateway_networks = gateway_networks

    def set_list_gateway_wallets_parameters(self, wallets, chain):
        self._list_gateway_wallets_parameters = {"wallets": wallets, "chain": chain}

    @property
    def prompt_text(self) -> str:
        return self.hummingbot_application.app.prompt_text

    @property
    def parser(self) -> ThrowingArgumentParser:
        return self.hummingbot_application.parser

    def get_subcommand_completer(self, first_word: str) -> Completer:
        subcommands: List[str] = self.parser.subcommands_from(first_word)
        return WordCompleter(subcommands, ignore_case=True)

    @property
    def _trading_pair_completer(self) -> Completer:
        trading_pair_fetcher = TradingPairFetcher.get_instance()
        market = next(
            (
                exchange
                for exchange in sorted(
                    list(AllConnectorSettings.get_connector_settings().keys()),
                    key=len,
                    reverse=True,
                )
                if exchange in self.prompt_text
            ),
            "",
        )
        trading_pairs = trading_pair_fetcher.trading_pairs.get(market, []) if trading_pair_fetcher.ready and market else []
        return WordCompleter(trading_pairs, ignore_case=True, sentence=True)

    @property
    def _gateway_chain_completer(self):
        return WordCompleter(self._gateway_chains, ignore_case=True)

    @property
    def _gateway_network_completer(self):
        return WordCompleter(self._gateway_networks, ignore_case=True)

    @property
    def _gateway_wallet_address_completer(self):
        return WordCompleter(list_gateway_wallets(self._list_gateway_wallets_parameters["wallets"], self._list_gateway_wallets_parameters["chain"]), ignore_case=True)

    @property
    def _option_completer(self):
        outer = re.compile(r"\((.+)\)")
        inner_str = outer.search(self.prompt_text)[1]
        options = inner_str.split("/") if "/" in inner_str else []
        return WordCompleter(options, ignore_case=True)

    @property
    def _config_completer(self):
        config_keys = self.hummingbot_application.configurable_keys()
        return WordCompleter(config_keys, ignore_case=True)

    def _complete_strategies(self, document: Document) -> bool:
        return "strategy" in self.prompt_text and "strategy file" not in self.prompt_text

    def _complete_pmm_script_files(self, document: Document) -> bool:
        return "PMM script file" in self.prompt_text

    def _complete_configs(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return "config" in text_before_cursor

    def _complete_options(self, document: Document) -> bool:
        return "(" in self.prompt_text and ")" in self.prompt_text and "/" in self.prompt_text

    def _complete_exchanges(self, document: Document) -> bool:
        return any(x for x in ("exchange name", "name of exchange", "name of the exchange")
                   if x in self.prompt_text.lower())

    def _complete_derivatives(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return "perpetual" in text_before_cursor or \
               any(x for x in ("derivative connector", "derivative name", "name of derivative", "name of the derivative")
                   if x in self.prompt_text.lower())

    def _complete_connect_options(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("connect ")

    def _complete_exchange_amm_connectors(self, document: Document) -> bool:
        return "(Exchange/AMM/CLOB)" in self.prompt_text

    def _complete_exchange_clob_connectors(self, document: Document) -> bool:
        return "(Exchange/AMM/CLOB)" in self.prompt_text

    def _complete_spot_exchanges(self, document: Document) -> bool:
        return "spot" in self.prompt_text

    def _complete_lp_connector(self, document: Document) -> bool:
        return "LP" in self.prompt_text

    def _complete_trading_timeframe(self, document: Document) -> bool:
        return any(x for x in ("trading timeframe", "execution timeframe")
                   if x in self.prompt_text.lower())

    def _complete_export_options(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return "export" in text_before_cursor

    def _complete_balance_options(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("balance ")

    def _complete_history_arguments(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("history ")

    def _complete_gateway_connect_arguments(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("gateway connect ")

    def _complete_gateway_connector_tokens_arguments(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("gateway connector-tokens ")

    def _complete_gateway_approve_tokens_arguments(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("gateway approve-tokens ")

    def _complete_gateway_arguments(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("gateway ") and not text_before_cursor.startswith("gateway config ")

    def _complete_gateway_config_arguments(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("gateway config ")

    def _complete_script_strategy_files(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("start --script ")

    def _complete_trading_pairs(self, document: Document) -> bool:
        return "trading pair" in self.prompt_text

    def _complete_paths(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return (("path" in self.prompt_text and "file" in self.prompt_text) or
                "import" in text_before_cursor)

    def _complete_gateway_chain(self, document: Document) -> bool:
        return "Which chain do you want" in self.prompt_text

    def _complete_gateway_network(self, document: Document) -> bool:
        return "Which network do you want" in self.prompt_text

    def _complete_gateway_wallet_addresses(self, document: Document) -> bool:
        return "Select a gateway wallet" in self.prompt_text

    def _complete_command(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return " " not in text_before_cursor and len(self.prompt_text.replace(">>> ", "")) == 0

    def _complete_subcommand(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        index: int = text_before_cursor.index(' ')
        return text_before_cursor[:index] in self.parser.commands

    def _complete_balance_limit_exchanges(self, document: Document):
        text_before_cursor: str = document.text_before_cursor
        command_args = text_before_cursor.split(" ")
        return len(command_args) == 3 and command_args[0] == "balance" and command_args[1] == "limit"

    def _complete_rate_oracle_source(self, document: Document):
        return all(x in self.prompt_text for x in ("source", "rate oracle"))

    def _complete_mqtt_arguments(self, document: Document) -> bool:
        text_before_cursor: str = document.text_before_cursor
        return text_before_cursor.startswith("mqtt ")

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        """
        Get completions for the current scope. This is the defining function for the completer
        :param document:
        :param complete_event:
        """
        if self._complete_pmm_script_files(document):
            yield from self._py_file_completer.get_completions(document, complete_event)
        elif self._complete_script_strategy_files(document):
            yield from self._script_strategy_completer.get_completions(
                document, complete_event
            )
        elif self._complete_paths(document):
            yield from self._path_completer.get_completions(document, complete_event)
        elif self._complete_strategies(document):
            yield from self._strategy_completer.get_completions(document, complete_event)
        elif self._complete_gateway_chain(document):
            yield from self._gateway_chain_completer.get_completions(
                document, complete_event
            )
        elif self._complete_gateway_network(document):
            yield from self._gateway_network_completer.get_completions(
                document, complete_event
            )
        elif self._complete_gateway_wallet_addresses(document):
            yield from self._gateway_wallet_address_completer.get_completions(
                document, complete_event
            )
        if self._complete_lp_connector(document):
            yield from self._evm_amm_lp_completer.get_completions(document, complete_event)
        elif self._complete_exchange_amm_connectors(document):
            if self._complete_spot_exchanges(document):
                yield from self._spot_exchange_completer.get_completions(
                    document, complete_event
                )
            else:
                yield from self._exchange_amm_completer.get_completions(
                    document, complete_event
                )
        elif self._complete_exchange_clob_connectors(document):
            if self._complete_spot_exchanges(document):
                yield from self._spot_exchange_completer.get_completions(
                    document, complete_event
                )
            elif self._complete_derivatives(document):
                yield from self._derivative_exchange_completer.get_completions(
                    document, complete_event
                )
            else:
                yield from self._exchange_clob_completer.get_completions(
                    document, complete_event
                )
        elif self._complete_spot_exchanges(document):
            yield from self._spot_exchange_completer.get_completions(
                document, complete_event
            )
        elif self._complete_trading_timeframe(document):
            yield from self._trading_timeframe_completer.get_completions(
                document, complete_event
            )
        elif self._complete_connect_options(document):
            yield from self._connect_option_completer.get_completions(
                document, complete_event
            )
        elif self._complete_export_options(document):
            yield from self._export_completer.get_completions(document, complete_event)
        elif self._complete_balance_limit_exchanges(document):
            yield from self._connect_option_completer.get_completions(
                document, complete_event
            )
        elif self._complete_balance_options(document):
            yield from self._balance_completer.get_completions(document, complete_event)
        elif self._complete_history_arguments(document):
            yield from self._history_completer.get_completions(document, complete_event)
        elif self._complete_gateway_connect_arguments(document):
            yield from self._gateway_connect_completer.get_completions(
                document, complete_event
            )
        elif self._complete_gateway_connector_tokens_arguments(document):
            yield from self._gateway_connector_tokens_completer.get_completions(
                document, complete_event
            )
        elif self._complete_gateway_approve_tokens_arguments(document):
            yield from self._gateway_approve_tokens_completer.get_completions(
                document, complete_event
            )
        elif self._complete_gateway_arguments(document):
            yield from self._gateway_completer.get_completions(document, complete_event)
        elif self._complete_gateway_config_arguments(document):
            yield from self._gateway_config_completer.get_completions(
                document, complete_event
            )
        elif self._complete_derivatives(document):
            if self._complete_exchanges(document):
                yield from self._derivative_exchange_completer.get_completions(
                    document, complete_event
                )
            elif "(Exchange/CLOB)" in self.prompt_text:
                yield from self._derivative_completer.get_completions(document, complete_event)
            else:
                yield from self._derivative_completer.get_completions(document, complete_event)
        elif self._complete_exchanges(document):
            yield from self._exchange_completer.get_completions(document, complete_event)
        elif self._complete_trading_pairs(document):
            yield from self._trading_pair_completer.get_completions(
                document, complete_event
            )
        elif self._complete_command(document):
            yield from self._command_completer.get_completions(document, complete_event)
        elif self._complete_configs(document):
            yield from self._config_completer.get_completions(document, complete_event)
        elif self._complete_options(document):
            yield from self._option_completer.get_completions(document, complete_event)
        elif self._complete_rate_oracle_source(document):
            yield from self._rate_oracle_completer.get_completions(
                document, complete_event
            )
        elif self._complete_mqtt_arguments(document):
            yield from self._mqtt_completer.get_completions(document, complete_event)
        else:
            text_before_cursor: str = document.text_before_cursor
            try:
                first_word: str = text_before_cursor[:text_before_cursor.index(' ')]
            except ValueError:
                return
            subcommand_completer: Completer = self.get_subcommand_completer(first_word)
            if complete_event.completion_requested or self._complete_subcommand(document):
                yield from subcommand_completer.get_completions(document, complete_event)


def load_completer(hummingbot_application):
    return HummingbotCompleter(hummingbot_application)
