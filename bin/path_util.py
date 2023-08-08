#!/usr/bin/python

import sys
if "hummingbot-dist" in __file__:
    # Dist environment.
    import os
    sys.path.append(sys.path.pop(0))
    sys.path.insert(0, os.getcwd())

    import hummingbot
    hummingbot.set_prefix_path(os.getcwd())
else:
    # Dev environment.
    from os.path import join, realpath
    sys.path.insert(0, realpath(join(__file__, "../../")))
