# NILO datalake en una VM

Solo hace falta ejecutar scripts en `~/nilo-datalake`. No uses `sg docker` ni `docker login` a mano: `./deploy.sh` y `./update.sh` aplican el grupo docker y el login GHCR si lo dejaste en `credentials.env`.

## Primera vez

```bash
mkdir -p ~/nilo-datalake && cd ~/nilo-datalake
curl -fsSL https://raw.githubusercontent.com/NeoVisionsAI/NILO-datalake/main/deploy/vm-ghcr/bootstrap.sh -o bootstrap.sh
chmod +x bootstrap.sh
./bootstrap.sh
./configure.sh    # contraseñas; opciones 8–9 si el paquete GHCR es privado
./deploy.sh       # o ./update.sh (bootstrap + deploy)
```

## Cada cambio en el repositorio

```bash
cd ~/nilo-datalake
./bootstrap.sh    # actualiza compose y scripts desde GitHub
./deploy.sh       # nueva imagen y servicios
```

Atajo equivalente: `./update.sh` (hace bootstrap y deploy).

Para revisar credenciales o probar la consola: `./configure.sh` (opción `w`).

La consola queda en `http://<ip>:8088/console`. MinIO guarda sesiones y dumps de base de datos. No hay MongoDB en este stack.

## Arranque automático

Tras un `./deploy.sh` correcto, el script intenta instalar la unidad systemd `nilo-datalake-vm.service` (salvo que pongas `NILO_INSTALL_SYSTEMD=0`). Eso levanta Docker Compose al iniciar el PC y lo reinicia si falla. Los contenedores ya llevan `restart: always`.

En la consola, **Device registry** configura el envío periódico de la IP pública a tu backend (registro tipo DNS de NAS, nilo-node, etc.).
