#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
# 設定は app/config.py が /data/options.json と環境変数から読み込む。
# with-contenv 経由で SUPERVISOR_TOKEN が環境変数に入る（ログには出さない）。
cd /srv
bashio::log.info "Starting Muse Automation MCP"
exec python -m app.main
