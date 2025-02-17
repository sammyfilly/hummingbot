import logging

import pandas as pd
import decimal

FLOAT_PRINTOUT_PRECISION = 8


def format_decimal(n):
    """
    Convert the given float to a string without scientific notation
    """
    try:
        with decimal.localcontext() as ctx:
            if isinstance(n, float):
                n = ctx.create_decimal(n)
            if not isinstance(n, decimal.Decimal):
                return str(n)
            n = round(n, FLOAT_PRINTOUT_PRECISION)
            return format(n.normalize(), 'f')
    except Exception as e:
        logging.getLogger().error(str(e))


pd.options.display.float_format = lambda x: format_decimal(x)
