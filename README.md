# JeroCoin

Proyecto Flask + SQLite para gestionar equipos, ciclos, contratos, entregas, revisiones e historial de JeroCoin.

## Inicio rápido

1. Abrí una terminal dentro de la carpeta del proyecto.
2. Activá tu entorno virtual.
3. Instalá dependencias:

```bash
pip install -r requirements.txt
```

4. Si necesitás reconstruir una base limpia para pruebas:

```bash
python seed_demo.py
```

Ese script deja una instalación mínima, sin cuentas demo de equipos, y crea solo una cuenta administrativa inicial. Podés personalizarla con variables de entorno antes de ejecutarlo:

```bash
set JEROCOIN_ADMIN_USER=jerocoin_admin
set JEROCOIN_ADMIN_PASSWORD=JeroCoin-Admin-2026
```

5. Levantá la app:

```bash
python app.py
```

6. Abrí:

```text
http://127.0.0.1:5000
```

## Credenciales iniciales

Las credenciales de trabajo se entregan por administración y ya no se muestran en la interfaz pública.
Si reconstruís la base con `seed_demo.py`, el script imprime en consola el usuario administrador creado.

## Variables de entorno útiles

- `JEROCOIN_SECRET_KEY`: clave de sesión para Flask.
- `JEROCOIN_OPENAI_API_KEY`: clave compartida para el asistente de IA.
- `JEROCOIN_AI_MODEL`: modelo a usar en el asistente, si querés sobrescribirlo.
- `JEROCOIN_DATABASE` o `JEROCOIN_DB`: ruta del archivo SQLite.

Por compatibilidad, el proyecto también acepta las variables heredadas `PANCHICOIN_OPENAI_API_KEY`, `PANCHICOIN_AI_MODEL`, `PANCHICOIN_DATABASE` y `PANCHICOIN_DB`.

## Notas

- El proyecto prioriza `jerocoin.db` si existe. Si no, reutiliza `panchicoin.db` automáticamente para no romper instalaciones previas.
- Los equipos pueden crear nuevas cuentas desde el panel admin cuando corresponda.
- Si usás el asistente de IA, la API key debe configurarse como variable de entorno del servidor.
