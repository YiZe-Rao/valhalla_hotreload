#!/bin/sh
set -e

# Default to 8080 if PORT is not set
PORT=${PORT:-8080}
echo "Starting Valhalla on port $PORT..."

# Patch the config to listen on the correct port
if [ -f /custom_files/valhalla.json ]; then
    jq ".httpd.service.listen = \"tcp://*:$PORT\"" /custom_files/valhalla.json > /custom_files/valhalla.json.tmp && \
    mv /custom_files/valhalla.json.tmp /custom_files/valhalla.json
fi

# Exec the service
exec valhalla_service /custom_files/valhalla.json 1
