# Política de seguridad

> **English:** please report security issues privately through GitHub's
> [private vulnerability reporting](https://github.com/subtitulos-castellano/bajar-subtitulos-esp/security/advisories/new),
> not in a public issue. Only the latest release is supported.

## Versiones con soporte

Solo se corrige la última versión publicada en
[Releases](https://github.com/subtitulos-castellano/bajar-subtitulos-esp/releases). Si usas una
anterior, actualiza antes de informar (`npx skills update` o `git pull` en la carpeta de la
skill).

## Cómo avisar de un problema de seguridad

Avisa **en privado**, no en una issue pública:

1. Entra en la pestaña **Security** del repositorio y pulsa **Report a vulnerability**
   ([enlace directo](https://github.com/subtitulos-castellano/bajar-subtitulos-esp/security/advisories/new)).
2. Explica qué ocurre, con qué versión y cómo reproducirlo. Si hace falta un archivo de
   ejemplo (un subtítulo o un vídeo manipulado), descríbelo o genéralo con un script pequeño;
   no adjuntes material con derechos de autor.

Es un proyecto mantenido por voluntarios: se responde en cuanto sea posible y, si se
confirma el problema, se publica una versión corregida y se menciona en sus notas.

## Qué entra en el alcance

- El script `bajar_subs.py`: lectura de pistas incrustadas en MKV/MP4, descarga, validación y
  guardado de subtítulos.
- Las instrucciones de la skill (`SKILL.md`), incluido cómo debe tratar el agente el texto de
  terceros.

Fuera del alcance: fallos de los proveedores de subtítulos, de [subliminal](https://github.com/Diaoul/subliminal) u otras
dependencias (avisa a sus autores) y de los reproductores de vídeo.

Las medidas de seguridad de la skill se describen en la sección
[Seguridad del README](README.md#seguridad).
