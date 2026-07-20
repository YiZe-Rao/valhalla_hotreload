@echo off
cd traffic_pipeline
docker build -t traffic-pipeline:latest .
docker run -it --rm -v "%CD%\data:/app/data" -v "%CD%\config.yaml:/app/config.yaml" -e VALHALLA_SERVICE_URL="http://host.docker.internal:8080" traffic-pipeline:latest