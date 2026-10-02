# NILO datalake en una VM

Primera vez: `./bootstrap.sh`, `./configure.sh`, `docker login ghcr.io` si el paquete es privado, `./update.sh`.

Después de cada push a `main`: `./update.sh`.

La consola queda en `http://<ip>:8088/console`. MinIO guarda las sesiones y los ficheros de copia de la base de datos. No hay conexión a MongoDB.

En `./configure.sh`: `m` instala MinIO si no está, `w` comprueba que la consola responde y que el usuario entra.
