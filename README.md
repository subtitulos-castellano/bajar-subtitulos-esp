# bajar-subtitulos-esp

> **English:** an agent skill for Claude Code, Codex and OpenCode that finds **Spanish from Spain
> (castellano) subtitles** for English-language movies and TV shows. It checks embedded MKV/MP4
> tracks first, then searches subtitle providers and verifies the dialect by reading the text,
> so Latin American Spanish mislabeled as "es" is not picked. Install with
> `npx skills add subtitulos-castellano/bajar-subtitulos-esp -g`. Documentation below is in Spanish.

Skill para agentes de programación ([Claude Code](https://claude.com/claude-code),
[Codex](https://developers.openai.com/codex) y [OpenCode](https://opencode.ai)) que consigue
**subtítulos en castellano (español de España)** para películas y series en inglés, con un
script de Python que hace todo el trabajo de forma autónoma.

Sigue el formato estándar de *Agent Skills* (`SKILL.md` con `name` y `description`), así que
el mismo repositorio sirve para los tres agentes sin cambiar nada.

Basta con pedirle al agente *"baja subtítulos"* en una carpeta: busca todos los vídeos,
mira primero si el propio archivo trae subtítulos en castellano, si no los busca en internet,
**comprueba leyendo el texto** que son castellano de España (y no latino mal etiquetado) y deja
un `.srt` junto a cada vídeo con su mismo nombre, listo para que el reproductor lo cargue solo.

## Características

- **Castellano de España, no latino.** Las etiquetas de idioma de las webs no son fiables
  (latinos marcados como "es", episodios mal subidos). El script decide el dialecto por el
  vocabulario: `vosotros`, `os`, `vale`, `chaval`, `-áis/-éis`… frente a `ustedes`,
  `computadora`, `celular`, `carro`, `ahorita`…
- **Primero mira dentro del vídeo.** Los WEB-DL de Disney+, Netflix, etc. suelen traer
  ~30 pistas de subtítulos incrustadas, castellano incluido, incluso el día del estreno, cuando
  aún no hay nada en internet. El script las lee **sin ffmpeg ni mkvtoolnix**:
  - **MKV**: parser EBML propio que salta vídeo y audio (~3 s para 3 GB).
  - **MP4 / M4V / MOV**: lee solo la caja `moov` y salta directamente a las muestras de
    subtítulos `tx3g` o WebVTT (~1 s para 10 GB). Respeta listas de edición y excluye pistas de
    capítulos.
  - Ignora pistas "forced" y distingue variantes por etiqueta (`es-ES`, `es-419`, nombres
    como "LA", "Latino", "España"…). En pistas oficiales la etiqueta latina prevalece sobre
    el detector de dialecto.
- **Varios proveedores** vía [subliminal](https://github.com/Diaoul/subliminal): OpenSubtitles
  (.org, con cuenta opcional), Addic7ed, Gestdown y Subtitulamos. Con cuenta, también
  opensubtitles.com, pero solo como último recurso (ver la instalación manual).
  Descarga todos los candidatos en español y se queda con el mejor castellano: primero el
  que se hizo para el mismo archivo o el mismo release (va sincronizado) y, entre los demás,
  el que tiene más señales de castellano.
- **Validación de cada candidato**: descarta subtítulos de otro episodio (según el nombre del
  release) o cuya duración no encaja con la del vídeo.
- **Plan B escalonado**: castellano › latino › inglés. Si solo hay latino, lo guarda (prefiriendo
  la pista incrustada, que va perfectamente sincronizada) y lo avisa claramente. Si no hay nada
  en español, baja inglés (primero la pista incrustada, prefiriendo SDH).
- **Nombre listo para el reproductor**: guarda `<video>.srt` en UTF-8, con el mismo nombre que
  el vídeo, para que VLC, MPC-HC, la tele, Plex… lo carguen automáticamente.
- **Re-ejecutable sin miedo**: detecta el idioma del `.srt` que ya existe; si es castellano no
  hace nada, si es latino o inglés vuelve a buscar castellano (suele aparecer días después del
  estreno). **Nunca sustituye un subtítulo por otro de un tipo peor**, y guarda copia de lo que
  reemplaza en `~/.subtitulos-esp/backup`.
- **Respetuoso con los proveedores**: como mucho 40 vídeos buscan en internet por ejecución
  (los que ya tienen castellano o lo traen incrustado no cuentan; el resto queda como
  `PENDIENTE (límite)` para la siguiente), descarga como mucho 5 candidatos por vídeo
  alternando proveedores y hace una pausa de 3 s entre vídeos.
- **Robusto con nombres raros**: toma el nombre de la serie del archivo y no de la carpeta
  (una carpeta `<SERIE> - FULL` hacía que la serie se buscara como "<SERIE> FULL").
- **Migra el formato antiguo**: renombra los `.es.srt` / `.en.srt` de versiones anteriores a
  `<video>.srt`.

## Cómo funciona

```
para cada vídeo de la carpeta (recursivo, ignora "sample"):
  1. ¿Ya hay <video>.srt en castellano?  → no hace nada
  2. Pistas incrustadas (MKV/MP4)        → si hay castellano, lo extrae y termina
  3. Proveedores en internet             → descarga candidatos en español, comprueba
                                           dialecto, episodio y duración
  4. Elige: castellano › latino (incrustado › web) › inglés (incrustado › web)
  5. Guarda <video>.srt (si mejora lo que hubiera) y lo apunta en el RESUMEN
```

Al final muestra un resumen con el tipo de cada subtítulo:

```
==== RESUMEN ====
CASTELLANO (incrustado)      pista 7 Spanish (Spain)        Pelicula.2026.1080p.WEB-DL.mp4
CASTELLANO                   addic7ed 12345                 Serie.S01E02.1080p.WEB.mkv
LATINO                       opensubtitles 1234567890       Serie.S01E03.1080p.WEB.mkv
INGLÉS (incrustado)          pista 3 English SDH            Serie.S01E04.1080p.WEB.mkv
```

## Instalación

Requisitos: Python 3.9+ y al menos uno de estos agentes: Claude Code, Codex u OpenCode.
Funciona en Windows, Linux y macOS. No hay que editar ninguna ruta: todo se guarda en la
carpeta personal de cada usuario (`~/.subtitulos-esp`).

### Instalación rápida

Con Node.js instalado, un solo comando la instala para tus agentes con
[`skills`](https://skills.sh) (te pregunta en cuáles):

```
npx skills add subtitulos-castellano/bajar-subtitulos-esp -g
```

Para elegir los agentes directamente: `-a claude-code -a codex -a opencode`. La primera vez
que uses la skill, el agente crea el entorno de Python que necesita. Si quieres usar
OpenSubtitles, sigue el paso 3 de la instalación manual.

### Instalación manual

1. Clona el repositorio en la carpeta de skills de tu agente. Cada uno busca las skills en
   un sitio distinto:

   | Agente      | Carpeta de skills del usuario                                        |
   |-------------|----------------------------------------------------------------------|
   | Claude Code | `~/.claude/skills/`                                                  |
   | Codex       | `~/.agents/skills/`                                                  |
   | OpenCode    | `~/.config/opencode/skills/` (y también lee `~/.claude/skills/` y `~/.agents/skills/`) |

   Para tenerla en los tres con una sola copia, clónala en `~/.claude/skills` (Claude Code y
   OpenCode la leen ahí) y enlázala desde `~/.agents/skills` para Codex:
   - Linux/macOS:
     ```
     git clone https://github.com/subtitulos-castellano/bajar-subtitulos-esp ~/.claude/skills/bajar-subtitulos-esp
     mkdir -p ~/.agents/skills
     ln -s ~/.claude/skills/bajar-subtitulos-esp ~/.agents/skills/bajar-subtitulos-esp
     ```
   - Windows (PowerShell; la unión de directorios no necesita permisos de administrador):
     ```
     git clone https://github.com/subtitulos-castellano/bajar-subtitulos-esp "$HOME\.claude\skills\bajar-subtitulos-esp"
     New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null
     New-Item -ItemType Junction -Path "$HOME\.agents\skills\bajar-subtitulos-esp" -Target "$HOME\.claude\skills\bajar-subtitulos-esp"
     ```
   Si solo usas uno de los agentes, basta con clonarla en su carpeta.
2. Crea el entorno virtual con subliminal:
   - Windows (PowerShell):
     ```
     python -m venv "$HOME\.subtitulos-esp\venv"
     & "$HOME\.subtitulos-esp\venv\Scripts\pip.exe" install -r "$HOME\.claude\skills\bajar-subtitulos-esp\requirements.txt"
     ```
   - Linux/macOS:
     ```
     python3 -m venv ~/.subtitulos-esp/venv
     ~/.subtitulos-esp/venv/bin/pip install -r ~/.claude/skills/bajar-subtitulos-esp/requirements.txt
     ```
   (Si te lo saltas, el agente lo crea la primera vez que uses la skill.)
3. (Opcional) Cuentas de [opensubtitles.org](https://www.opensubtitles.org) y de
   [opensubtitles.com](https://www.opensubtitles.com) para obtener más resultados (son cuentas
   distintas; puedes poner una, las dos o ninguna). Crea tú mismo el archivo
   `~/.subtitulos-esp/credenciales.json` (queda fuera del repositorio y nunca se sube):
   ```json
   {"opensubtitles": {"username": "<usuario>", "password": "<contraseña>"},
    "opensubtitlescom": {"username": "<usuario>", "password": "<contraseña>"}}
   ```
   opensubtitles.com solo se usa como **último recurso**: se le piden subtítulos únicamente si los
   demás proveedores no han encontrado castellano (o, al final, inglés), porque sus cuentas solo
   permiten unas pocas descargas al día. Se usa el nombre de usuario, no el email. Cuando se agota
   el cupo, el script lo dice y sigue con los demás.

## Uso

Pídeselo al agente con tus palabras (*"baja subtítulos de esta carpeta"*) o invoca la skill
por su nombre:

| Agente      | Invocación explícita                     |
|-------------|------------------------------------------|
| Claude Code | `/bajar-subtitulos-esp <carpeta>`        |
| Codex       | `$bajar-subtitulos-esp <carpeta>`        |
| OpenCode    | *"usa la skill bajar-subtitulos-esp con <carpeta>"* |

El script necesita acceso a internet para consultar los proveedores. Si tu agente ejecuta
los comandos en un sandbox sin red (Codex, por ejemplo, según su configuración), tendrás que
aprobar que lo ejecute con acceso a la red.

O directamente:

```
python -I bajar_subs.py <carpeta_o_video> [opciones]
```

| Opción              | Efecto                                                                 |
|---------------------|------------------------------------------------------------------------|
| `--dry-run`         | No escribe nada; muestra qué haría                                     |
| `--force`           | Rehace aunque ya haya castellano (nunca baja a un tipo peor)           |
| `--solo-castellano` | No guarda latino aunque no haya castellano                             |
| `--sin-ingles`      | No baja inglés cuando no hay nada en español                           |
| `--ingles`          | Además extrae la pista inglesa incrustada como `<video>.en.srt`        |
| `--max-videos N`    | Máximo de vídeos que buscan en internet (por defecto 40; `0` = sin límite) |
| `--max-descargas N` | Máximo de candidatos descargados por vídeo (por defecto 5; `0` = sin límite) |

## Archivos

| Archivo          | Contenido                                                            |
|------------------|----------------------------------------------------------------------|
| `SKILL.md`       | Instrucciones de la skill para el agente y lecciones aprendidas      |
| `bajar_subs.py`  | Script: lectura de pistas MKV/MP4, búsqueda, validación y guardado   |
| `requirements.txt` | Dependencias de Python                                             |
| `tests/test_seguridad.py` | Pruebas de seguridad con vídeos y textos manipulados        |
| `SECURITY.md`    | Política de seguridad y cómo avisar de un problema                   |
| `LICENSE`        | Licencia MIT                                                         |

## Uso responsable

Este proyecto no aloja ni distribuye subtítulos: solo los busca en proveedores públicos y
los descarga para los vídeos que ya tienes. Cada proveedor tiene sus propias condiciones de
uso; respétalas. OpenSubtitles solo se consulta con tu propia cuenta gratuita de
[opensubtitles.org](https://www.opensubtitles.org) o de [opensubtitles.com](https://www.opensubtitles.com)
(sin ellas, el script lo omite); .com, además, solo como último recurso por su cupo diario. No
lances búsquedas masivas que sobrecarguen los servicios: los límites por defecto (40 vídeos
por ejecución, 5 descargas por vídeo y una pausa entre vídeos) están para eso; si una
carpeta tiene más, deja que lo pendiente se procese en otra ejecución en lugar de quitar el
límite.

## Seguridad

La skill trata todo lo que llega de fuera como datos no fiables: los subtítulos descargados,
las pistas incrustadas en los vídeos, los nombres de pistas y de releases y las respuestas de
los proveedores.

- **Sin inyecciones**: no usa bases de datos, no ejecuta comandos del sistema ni `eval`, y no
  deserializa datos (`pickle`). El texto de un subtítulo solo se analiza con expresiones
  regulares y se guarda como texto, así que no puede colar SQL, comandos ni código.
- **Rutas seguras**: el `.srt` siempre toma el nombre de tu vídeo. Ningún dato de los
  proveedores interviene en las rutas, así que un subtítulo no puede escribir fuera de su sitio
  ni sobrescribir otros archivos. Lo que se sustituye se copia antes a `~/.subtitulos-esp/backup`.
- **Conexiones cifradas**: los proveedores se consultan por HTTPS con verificación de
  certificados.
- **Subtítulos limpios**: antes de guardarlos se quitan los caracteres de control, de ancho
  cero y de dirección del texto, y se descartan los de tamaño anormal (más de 2 millones de
  caracteres). Así se reduce el riesgo de subtítulos maliciosos preparados para atacar fallos
  de los reproductores, como los que se publicaron en 2017 para VLC, Kodi o Popcorn Time.
- **Lectura robusta de vídeos**: el lector de pistas MKV/MP4 tiene límites de tamaño y detecta
  archivos cortados o manipulados. Si encuentra uno, deja de leer ese vídeo y sigue con la
  búsqueda en internet, en lugar de colgarse o agotar la memoria.
- **Protección del agente**: los nombres de pistas y los identificadores se muestran en una
  sola línea y sin caracteres de control, y las instrucciones de la skill indican al agente
  que trate el texto de terceros como datos, nunca como instrucciones (*prompt injection*).
  Por descargar texto de terceros, la auditoría de Snyk en skills.sh marca la skill con riesgo
  medio (W011); es inherente a lo que hace la skill.
- **Credenciales**: las contraseñas de OpenSubtitles (.org y .com) se guardan en texto plano en
  `~/.subtitulos-esp/credenciales.json`, fuera del repositorio (`.gitignore` la excluye). Usa
  una contraseña exclusiva para cada cuenta.

Para comprobarlo tú mismo, ejecuta las pruebas de seguridad con el Python del entorno virtual:

```
python -I tests/test_seguridad.py
```

¿Has encontrado un problema de seguridad? Avisa en privado como se explica en
[SECURITY.md](SECURITY.md), no en una issue pública.

## Licencia

[MIT](LICENSE): puedes usar, copiar, modificar y redistribuir el código libremente,
manteniendo el aviso de copyright. Se ofrece tal cual, sin garantías.
