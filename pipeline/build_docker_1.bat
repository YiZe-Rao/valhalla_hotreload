@echo off
docker buildx build --platform linux/amd64 -t valhalla-local-test --load .
docker run -d -p 8080:8080 --name valhalla-test valhalla-local-test
docker cp valhalla-test:/custom_files/tiles/way_edges.txt ".\traffic_pipeline\data\road_data\way_edges.txt"