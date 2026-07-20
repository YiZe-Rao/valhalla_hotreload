@echo off
REM 1) Copy the traffic data to your project root (for future Docker builds)
xcopy "traffic_pipeline\data\output\stage5_speed_profile\traffic_data\*" "traffic_data\" /E /I /Y

REM 2) Copy it into the running container
docker cp "traffic_data\." valhalla-test:/custom_files/traffic_data

REM 3) Run the command inside the container
docker exec valhalla-test valhalla_add_predicted_traffic -c /custom_files/valhalla.json -t /custom_files/traffic_data

REM 4) Restart the container to pick up the new tiles
docker restart valhalla-test
