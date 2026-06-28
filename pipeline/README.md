
## Architecture Overview
```
+-------------------------------------------------------------+
|                      CONTAINER 1: Valhalla                   |
|   (Already running on port 8080)                             |
|                                                              |
|   - trace_attributes endpoint at http://localhost:8080       |
|   - Returns edge IDs for GPS traces                          |
+-------------------------------------------------------------+
                            |
                            | HTTP (trace_attributes)
                            V
+-------------------------------------------------------------+
|                   CONTAINER 2: Traffic Pipeline              |
|   (This framework)                                           |
|                                                              |
|   Input: Raw GPS data (CSV or Firestore)                     |
|                                                              |
|   +---------+  +---------+  +---------+  +---------+  +----+ |
|   |  Data   |->|  Map    |->| Speed   |->| Empty   |->|Spd | |
|   |  Clean  |  |Matching |  |Calcultn |  |SlotsFill|  |Prof| |
|   +---------+  +---------+  +---------+  +---------+  +----+ |
|                                                              |
|   Output: Speed CSV files in Valhalla historical format      |
+-------------------------------------------------------------+
```

## Run Valhalla locally / Build Docker 1

Prune first (Optional)
```bash
docker buildx prune -f
```

Build the Docker image (using buildx so it works on linux/amd64):
```bash
docker buildx build --platform linux/amd64 -t valhalla-local-test --load .
```

This command:

- Uses `docker buildx` to build with BuildKit and targets the `linux/amd64` platform so the image can run on that architecture.
- Tags the image as `valhalla-local-test` and uses `--load` to load the built image into the local Docker image store so it can be run with `docker run`.

Start a container from the built image:

```bash
docker run -d -p 8080:8080 --name valhalla-test valhalla-local-test
```

This command:

- Runs the container in the background with `-d` and maps host port `8080` to container port `8080` with `-p 8080:8080`.
- Names the container `valhalla-test` and uses the previously built `valhalla-local-test` image.

After the container starts, the service should be available on `http://localhost:8080`.

Run this command to copy the way to edges mapping file:

```
docker cp valhalla-test:/custom_files/tiles/way_edges.txt ".\traffic_pipeline\data\road_data\way_edges.txt"
```

You can stop and remove the container with:

```bash
docker stop valhalla-test
docker rm valhalla-test
```

### IMPORTANT
- Executing command `RUN valhalla_ways_to_edges -c /custom_files/valhalla.json` in Dockerfile produces the file `way_edges.txt`. This should be the same file as in `traffic_pipeline/data/road_data/way_edges.txt`. This ensures that the mapping of the OSM way IDs and the Valhalla graph IDs is correct.

## Installation of Docker 2

Go to the traffic_pipeline directory
```bash
cd traffic_pipeline
```

Build the docker image for docker 2.
```bash
docker build -t traffic-pipeline:latest .
```

## Usage

```bash
docker run -it --rm \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -e VALHALLA_SERVICE_URL="http://host.docker.internal:8080" \
  traffic-pipeline:latest
```

**On Windows**
```
docker run -it --rm -v "%CD%\data:/app/data" -v "%CD%\config.yaml:/app/config.yaml" -e VALHALLA_SERVICE_URL="http://host.docker.internal:8080" traffic-pipeline:latest
```


## Using the Traffic Data
JUST RUN `update_traffic.bat` in terminal.

Step-by-step Process:
1. Copy the traffic data to your project root (for future Docker builds)

```
xcopy "traffic_pipeline\data\output\stage5_speed_profile\traffic_data\*" "traffic_data\" /E /I /Y
```

2. Copy it into the running container:

```
docker cp "traffic_data\." valhalla-test:/custom_files/traffic_data
```

3. Run the command inside the container:

```
docker exec valhalla-test valhalla_add_predicted_traffic -c /custom_files/valhalla.json -t /custom_files/traffic_data
```

After that, restart the container to pick up the new tiles:
```
docker restart valhalla-test
```