#!/usr/bin/env sh
# Starts the GOC Register Data Collector dashboard and opens it in the browser.
cd "$(dirname "$0")" && python3 -m goc_collector serve
