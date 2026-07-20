FROM ghcr.io/valhalla/valhalla-scripted@sha256:e0015e20b3f2d5369d5a0a945a671ee5829510e8fa2ee840d92aa7fb6047ea0d AS builder
WORKDIR /custom_files

# 1. Copy config files
COPY ./custom_files /custom_files
COPY ./traffic_data /custom_files/traffic_data

# 2. Download Hong Kong OSM data
# (Assumes curl is available in the base image)
RUN curl -L -o /custom_files/hong-kong-latest.osm.pbf http://download.geofabrik.de/asia/china/hong-kong-latest.osm.pbf

# 3. Create necessary directories
RUN mkdir -p /custom_files/tiles /custom_files/transit_tiles /custom_files/elevation_data

# 4. Build Admin and Timezone databases
RUN valhalla_build_timezones > /custom_files/timezones.sqlite
RUN valhalla_build_admins -c /custom_files/valhalla.json /custom_files/hong-kong-latest.osm.pbf

# 5. Build Tiles
RUN valhalla_build_tiles -c /custom_files/valhalla.json /custom_files/hong-kong-latest.osm.pbf

# 6. Convert Ways to Edges (maps OSM way IDs to graph IDs for traffic data)
RUN valhalla_ways_to_edges -c /custom_files/valhalla.json

# 7. Add Predicted Traffic
RUN valhalla_add_predicted_traffic -c /custom_files/valhalla.json -t /custom_files/traffic_data

###############################################################################
# Stage 2: Runtime
###############################################################################
FROM ghcr.io/valhalla/valhalla-scripted@sha256:e0015e20b3f2d5369d5a0a945a671ee5829510e8fa2ee840d92aa7fb6047ea0d
WORKDIR /custom_files

# Copy configuration and artifacts
COPY --from=builder /custom_files/valhalla.json ./
COPY --from=builder /custom_files/timezones.sqlite ./
# Attempt to copy admins.sqlite, if build failed this will error. 
# If Hong Kong extract is too small for admin DB, remove this line.
COPY --from=builder /custom_files/admins.sqlite ./
COPY --from=builder /custom_files/tiles ./tiles/
COPY --from=builder /custom_files/entrypoint.sh /usr/local/bin/entrypoint.sh

# Ensure script is executable
RUN chmod +x /usr/local/bin/entrypoint.sh

# Cloud Run expects 8080 by default
ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]