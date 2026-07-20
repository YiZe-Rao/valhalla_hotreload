(Valhalla)[https://github.com/valhalla/valhalla] is a ruoting engine using OpenStreetMap, it is able to route based on OpenStreetMap, returning turn by turn insturction, polyline, distance and time. It is also possible to overlay our own route/time data on to the map and get more accurate time estimate

# Why are we using valhalla
- Cheaper than using google map
- Locally hosted, faster speed
- Relatively accurate (see benchmark)

# Build 
[Default Valhalla Docker](https://github.com/valhalla/valhalla/) is harder to use, so we chose the [GISOPS version](ghcr.io/nilsnolde/docker-valhalla/valhalla) which has a few shall script and dockerfile written.

GISOPS valhall docker puts tiles and other files in an external folder (/custom_files) as a mount. Since we wanna make it more portable (e.g. run locally or run on Cloud Run instead of GKE), we put everything into single docker in build stage. 

## Docker Build
### Google Cloud Auth and choose project
``` bash
gcloud auth login
gcloud config set project dash-dev2-edcb3 
```
### Docker auth
Get access token from gcloud and pipe into docker login
``` bash
gcloud auth print-access-token \
  --impersonate-service-account github-action@dash-dev2-edcb3.iam.gserviceaccount.com | docker login \
  -u oauth2accesstoken \
  --password-stdin asia-east2-docker.pkg.dev 
```
### Building Docker
Build and push
``` bash
docker buildx build --platform linux/amd64 -t asia-east2-docker.pkg.dev/dash-dev2-edcb3/gcf-artifacts/valhalla:latest --push .
```
 ᕁ᙮ᕁᕽᕽᕁ᙮ OR ᙮ᕁᕽᕽᕁ᙮ᕁ

Build then push
``` bash
docker buildx build --platform linux/amd64 -t asia-east2-docker.pkg.dev/dash-dev2-edcb3/gcf-artifacts/valhalla:latest .
docker push asia-east2-docker.pkg.dev/dash-dev2-edcb3/gcf-artifacts/valhalla:latest
```
### Run locally
``` bash
docker run -p 8080:8080 asia-east2-docker.pkg.dev/dash-dev2-edcb3/gcf-artifacts/valhalla:latest
```

### Deploy to cloud run
``` bash
  gcloud run deploy valhalla \
  --image asia-east2-docker.pkg.dev/dash-dev2-edcb3/gcf-artifacts/valhalla:latest \
  --platform managed \
  --region asia-east2 \
  --allow-unauthenticated \
  --timeout=600
```
*To-do:*
`Use GKE`

### Test
Locally
``` bash
curl -X POST https://localhost:8080/route \
  -H "Content-Type: application/json" \
  -d '{
    "locations": [
      { "lat":  22.278627410710985, "lon": 114.18463939389214 },
      { "lat": 22.314387408225596, "lon": 113.9163352487539 }
    ],
    "costing": "auto",
    "directions_options": { "units": "kilometers" }
  }' | jq -r '.trip.legs[0].maneuvers[] | .instruction'
```

GCP Cloud Run
``` bash
curl -X POST https://valhalla-210975360305.asia-east2.run.app/route \
  -H "Content-Type: application/json" \
  -d '{
    "locations": [
      { "lat":  22.278627410710985, "lon": 114.18463939389214 },
      { "lat": 22.314387408225596, "lon": 113.9163352487539 }
    ],
    "costing": "auto",
    "directions_options": { "units": "kilometers" }
  }' | jq -r '.trip.legs[0].maneuvers[] | .instruction'
```

# Benchmark
## What we did
1. Download 100+ trips from production 
2. Do distance, time estimation using
    - Google map
    - Vahalla
    - Strightline
3. Compare time/distance estimate

## Result
Blub, Blub, Blub, Blub, Blub, Blub, Blub