#!/bin/bash
# Dvojklikem spustíš mapu. Okno Terminálu nech otevřené – dokud běží, data se aktualizují.
cd "$(dirname "$0")"
exec python3 server.py