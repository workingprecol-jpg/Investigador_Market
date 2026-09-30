# Investigación del radar — solo información

Ampliación solicitada el 26 de septiembre de 2026, conservando el diseño claro.
No crea órdenes, posiciones, fills ni simulaciones. Las reglas de entrada
15m/1h siguen siendo las documentadas en [SCANNER.md](SCANNER.md).
Estas fuentes aportan evidencia para revisión manual; no prueban rentabilidad.

## Gráfico y posicionamiento

Cada análisis Binance incluye hasta 100 velas cerradas de 15m, 1h, 4h y 1d,
si su consulta está disponible. El gráfico permite escoger intervalo, mostrar
30/60/100 velas y revisar OHLCV con ratón o selector de vela accesible por teclado.
Traza EMA20, volumen y VWAP aproximado con precio típico de vela, anclado al
inicio de la serie descargada. No es VWAP exacto de cada trade ni VWAP diario.
El resumen 1h/4h/1d compara EMA20/50 y cierre. 4h/1d se cachean hasta el nuevo
periodo; sus fallos no eliminan el análisis básico 15m/1h.

En futuros se consultan open interest y su cambio de unidades en 1h, flujo taker
comprador/vendedor en cuatro intervalos cerrados de 15m y últimos eventos de
funding. El delta USDT es una aproximación al precio actual. Open interest no
identifica dirección long/short; taker flow no es el libro de órdenes ni liquidez
futura. No se consulta una cuenta. Fuente: [Binance Futures](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data).

## Riesgos del token y descubrimiento

GoPlus aporta un informe para la dirección exacta del token DEX en redes
compatibles. En EVM se revisan honeypot, restricciones de venta, código abierto,
emisión, pausas, blacklist, proxy e impuestos. En Solana se revisan autoridades
de emisión/congelación y modificación de saldos. Venta desconocida permanece
desconocida. Los campos no disponibles nunca equivalen a ausencia de riesgo.

La concentración se calcula solo con los primeros diez holders disponibles,
excluyendo etiquetas conocidas de exchange, burn, pool, liquidez o locker;
no representa todos los holders ni prueba de control económico. El bloqueo y
propiedad de liquidez del par permanecen desconocidos. Honeypot o imposibilidad
de venta explícitos descartan un DEX; el resto del informe no certifica seguridad.
Fuente: [GoPlus EVM](https://docs.gopluslabs.io/reference/tokensecurityusingget_1)
y [GoPlus Solana](https://docs.gopluslabs.io/reference/solanatokensecurityusingget).

La lista puede ordenarse por actividad, fuerza 24h frente a BTC (diferencia en
puntos porcentuales) o aceleración de volumen (media de cuatro velas contra las
veinte anteriores). DEX compara liquidez con su observación anterior de hasta
24h cuando existe. Market cap/FDV del par vienen de DEX Screener; las monedas
principales pueden enriquecerse por IDs CoinGecko. Los valores faltantes quedan
vacíos. La puntuación de descubrimiento es heurística, no una probabilidad;
no activa spot, long ni short y no garantiza que una moneda vaya a subir.

## Grandes transacciones y calendario

Sin claves se consulta una muestra de las diez transacciones más recientes del
mempool Bitcoin y se muestran sumas de salidas estimadas en al menos un millón
USD, con enlace al explorador. Pueden incluir cambio, están pendientes y no
identifican dueño, compra, venta ni depósito en exchange. No ver eventos en
esta muestra no prueba ausencia de ballenas. Fuente: [mempool API](https://mempool.space/docs/api/rest#get-mempool-recent).

La conexión opcional Whale Alert REST Enterprise consulta una muestra de hasta
100 transacciones por red en los últimos bloques de Bitcoin, Ethereum y Solana.
Solo usa atribución del proveedor, excluye transferencias internas identificadas
y distingue entrada/salida de exchanges cuando hay etiquetas. Ninguna
transferencia prueba una venta o compra. Los fallos o redes sin cobertura se
indican; esta integración no se ha probado con una suscripción real.
Fuente: [Whale Alert](https://developer.whale-alert.io/api-account/documentation).

El calendario opcional CoinGlass muestra desbloqueos de los siguientes 30 días
de la primera página (hasta 250 activos). Es cobertura parcial; no se asigna
un activo a un contrato DEX solo por su símbolo. Sin clave o acceso del plan,
las fechas quedan desconocidas. Fuente: [CoinGlass unlocks](https://docs.coinglass.com/reference/coin-unlock-list).

## Claves opcionales

El análisis básico, GoPlus y la muestra BTC funcionan sin claves. El scanner
no carga `.env`. Lee únicamente estas variables del entorno de su proceso:

| Variable | Función | Disponibilidad |
|---|---|---|
| `WHALE_ALERT_API_KEY` | Atribución de transferencias | Suscripción REST Enterprise compatible |
| `COINGECKO_DEMO_API_KEY` | Market cap/FDV de monedas principales | [API Demo](https://docs.coingecko.com/demo/reference/coins-markets) |
| `COINGLASS_API_KEY` | Calendario de desbloqueos | Plan con acceso, Startup o superior según documentación |

No se contrató ningún plan ni se guardaron claves. Configurarlas localmente
fuera de archivos versionados y reiniciar la única tarea `FinalBoss-scanner`
para que su proceso herede el entorno. No pegarlas en chats. Los headers
de proveedor solo se permiten hacia su host; nunca se envían a Binance.
Las peticiones siguen siendo GET y no se importan clientes de trading.

## Historial observado

`bot/signal_history.py` guarda en `data/scanner.history.sqlite3` observaciones
generadas por este radar. Retiene análisis de los últimos 30 días y conserva
hipótesis de entrada con sus niveles y checklist originales, una por
moneda/mercado/lado/vela. No importa ni modifica bases de trading históricas.

Cada hipótesis se revisa con precios posteriores a 1h/4h/24h, con tolerancia
de hasta 15 minutos. Una observación tardía no rellena un resultado faltante.
Se registran excursiones favorables/adversas y niveles vistos en velas enteras
posteriores a la observación. Si ambos niveles aparecen en la misma vela, el
orden queda desconocido. Las excursiones tienen cobertura parcial y son
variaciones brutas sin costes; no equivalen a ganancias ni a un backtest completo.
Una señal inexistente no produce resultados inventados.

## Archivos

- `bot/market_research.py`: enriquecimientos de mercado independientes de ejecución.
- `bot/scanner.py`: selección, señales y publicación atómica del esquema 3.
- `bot/scanner.html`: gráficos y secciones de investigación sin scripts externos.
- `tests/test_market_research.py`: datos causales, fuentes incompletas y persistencia.

`bot/research.py` pertenece al motor histórico y permanece separado.
