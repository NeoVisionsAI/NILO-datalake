# NILO datalake en una VM

Primera vez: `./bootstrap.sh`, `./configure.sh`, `docker login ghcr.io` si el paquete es privado, `./update.sh`.

Después de cada push a `main`: `./update.sh`.

La consola queda en `http://<ip>:8088/console`. El mismo comando levanta la API, MongoDB y MinIO.
