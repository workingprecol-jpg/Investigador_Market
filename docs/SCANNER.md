# Buscador de oportunidades — solo análisis

Esta es la operación actual solicitada por el usuario. No abre trades paper,
Demo ni reales. El bloqueo de `run` está en código, incluso con otra
configuración o `--mode demo`; `ANALYSIS_ONLY` registra esa política.
Las bases y resultados de trading antiguos
se conservan como historia; no alimentan las señales del buscador.

```powershell
python -m bot scan --once
python -m bot scan
python -m bot scan-status
python -m bot scan-dashboard --port 8765
```

El proceso usa solo GET, no carga `.env`, no requiere claves para el análisis básico, no accede a la cuenta
ni importa el motor de ejecución. Consulta cada cinco minutos **después de
terminar el ciclo**. Una sola instancia escribe `data/scanner.json` de forma
atómica. Su log es `data/scanner.log`. El panel mantiene el puerto 8765 y solo
lee resultados locales. Sin datos recientes invalida las señales de entrada.

La ampliación de investigación añade gráficos 15m/1h/4h/1d, EMA/VWAP/volumen,
posicionamiento público de futuros, riesgos GoPlus, ranking de descubrimiento,
grandes transacciones BTC e historial observado. Tres integraciones opcionales
leen claves del entorno: Whale Alert (atribución), CoinGecko (valoraciones) y
CoinGlass (desbloqueos). Sin configurar muestran información desconocida.
Cobertura, interpretación y configuración en [RESEARCH.md](RESEARCH.md).

## Cobertura y selección

- Binance Spot: consulta el universo de pares USDT activos con permisos spot;
  excluye algunas stablecoins conocidas. Revisa cotizaciones de todos esos pares,
  preselecciona volumen de 24h de al menos 2 millones USDT y spread <=20 bps.
  Analiza velas cerradas de 15m y 1h para hasta 15 líderes de subida, otros 15
  pares en rotación y el seguimiento fijo. El contador distingue universo y
  análisis detallado, con un máximo de 45 símbolos deduplicados por ciclo.
- Binance Futures: contratos perpetuos activos con cotización y margen USDT.
  Revisa hasta ocho líderes de subida, siete de caída, quince en rotación y el
  seguimiento fijo (máximo 45). Usa sus propias cotizaciones, velas y funding;
  spot no sirve como sustituto para señalar long o short.
- Seguimiento fijo: BTC, ETH (Ethereum), SOL, BNB, XRP, ADA, DOGE, AVAX, LINK,
  DOT, LTC, TRX, SUI, TON y PEPE en ambos mercados. Si un mercado no existe o
  falla el análisis, conserva la fila en `ESPERAR`, sin inventar precio ni niveles.
  El futuro `1000PEPEUSDT` representa 1.000 PEPE; no se convierte a precio spot.
- DEX Screener: reúne perfiles recientes y actualizados, deduplica por red y
  dirección y consulta hasta 10 contratos por ciclo en rotación. Escoge el par de
  mayor liquidez donde el token es base. No equivale a descubrir todos los tokens
  nuevos, ni a cubrir todas las redes. Un perfil puede ser promocional.
- CoinDesk RSS: titulares de hasta seis horas, con fecha y enlace originales.
  Vinculación limitada por palabras, sin corroboración independiente ni análisis
  del artículo completo. Un feed fallido impide entradas condicionadas de spot,
  long y short. Un feed disponible no certifica ausencia de incidentes.

## Decisiones

`SPOT_CONDICIONAL` y `LONG_CONDICIONAL` exigen EMA20/50 alcistas en 15m y 1h,
cierre por encima de EMA20, ruptura de la resistencia de las 20 velas anteriores
y RSI14 entre 50 y 70. `SHORT_CONDICIONAL` exige las confirmaciones bajistas,
pérdida del soporte de las 20 velas previas y RSI14 entre 30 y 50.

Todos requieren volumen relativo de la vela cerrada >=1,5 veces la media previa,
precio actual manteniendo la ruptura y a <=0,5 ATR del cierre, volumen 24h
>=2 millones USDT, spread <=20 bps y consulta de contexto disponible. Se evita
perseguir extensión direccional >2,5 ATR respecto a EMA20 o movimientos absolutos
>25% en 24h. Invalidación a menos del 8% de la entrada y niveles positivos.
Futuros además exige funding observado hace <=180 segundos y funding adverso
<=0,1% por evento: positivo para long, negativo para short. No se define
apalancamiento ni tamaño; el funding futuro puede cambiar.

Cada escenario tiene su propia decisión y checklist: requisito, valor observado
y estado cumplido/pendiente. Cualquier requisito fallido mantiene `ESPERAR`
(liquidez insuficiente: `DESCARTAR`). El sesgo es el escenario con más condiciones
reunidas; no constituye una entrada. El panel añade el requisito de datos vigentes.
Estas señales son hipótesis para revisión manual; no garantizan seguridad de la
entrada y no se ha validado rentabilidad de las reglas.

Las referencias de entrada, invalidación (2 ATR, mínimo 0,5%) y objetivo 2R son
niveles informativos, sin órdenes ni garantía de ejecución; excluyen costes.
Para short la invalidación queda arriba y el objetivo abajo; para spot/long,
invalidación abajo y objetivo arriba. Se muestran también al esperar para entender
el escenario, sin que esos niveles por sí solos autoricen una entrada.
`ESPERAR` describe condiciones faltantes; `DESCARTAR`, debilidad o liquidez
insuficiente. DEX nunca recibe una señal de entrada: `INVESTIGAR` exige liquidez
>=100.000 USD y par de al menos 24h, pero siguen pendientes contrato, holders,
posibilidad de venta y otros riesgos. Un par antiguo no demuestra token seguro.

Las puntuaciones son porcentaje de condiciones cumplidas (spot/futuros) o actividad observada (DEX),
**no probabilidades** y no son comparables entre fuentes. Datos mayores de
15 minutos o un ciclo fallido degradan señales de entrada a `ESPERAR`.
Las observaciones DEX usan la hora de consulta: la API no verifica la hora de
última actualización del proveedor. La decisión final corresponde al usuario.

Fuentes oficiales: [Binance, datos públicos](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md),
[Binance Futures, datos de mercado](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data),
[DEX Screener API](https://docs.dexscreener.com/api/reference).

## Dirección del panel

El detalle de cada moneda muestra «¿Seguirá subiendo o bajando?» con un título
de continuidad bullish/bearish condicionada o sin dirección clara. Se basa en
EMA20/50 y cierre de las series propias de 1h y 4h; el precio observado también
debe permanecer del lado de EMA20. Un sesgo diario opuesto se presenta como
conflicto entre plazos. Nunca se deduce continuidad del lado `spot`/`long`/`short`,
del cambio de 24h ni de la puntuación de entrada.

Se muestran ventanas de revisión de 1h, 4h y 24h con fecha y hora en Colombia,
ancladas a la observación del escaneo; refrescar el navegador no mueve las fechas.
Cada ventana usa el contexto 1h/4h/1d correspondiente y ofrece las condiciones,
EMA y fecha de la vela cerrada. Son horizontes de seguimiento, **no una estimación
validada de duración ni fechas pronosticadas de reversión**. Datos de más de
15 minutos, desconexión, errores o precio inválido retiran continuidad y fechas;
un contexto faltante, futuro o antiguo invalida su propio horizonte.
DEX sin esas series muestra continuidad y fechas desconocidas.

Ampliación autorizada: conservar el diseño y añadir gráfico, posicionamiento,
ballenas, riesgos del token, descubrimiento e historial de señales.
Intent: revisar evidencia por moneda antes de decidir manualmente.
Hierarchy: gráfico y decisión comparten el detalle; nuevos datos se agrupan en
disclosures de posicionamiento, contrato e historial para mantener la lista clara.
Palette/Depth/Surfaces: reutilizar papel, blanco, grafito, verde y ámbar existentes;
bordes suaves, sin nueva estética ni colores de decoración.
Typography: Segoe UI y números tabulares; cifras legibles, metadatos secundarios.
Spacing: unidad 4px, controles nativos >=40px, gráfico adaptable al ancho.
Componentes: gráfico SVG local con controles de intervalo y ventana y selector
de vela accesible; tabla de descubrimiento ordenable; listas de eventos con
fuente/fecha; historial observado, sin botones para operar.

Intent: propietario que busca oportunidades y decide manualmente; claridad sobre
por qué esperar y cuándo falta evidencia.
Hierarchy: lista de oportunidades y decisión seleccionada dominan; cobertura y
fuentes aportan contexto, sin métricas de ganancias ficticias.
Palette: papel cálido, superficies blancas, tinta grafito, verde para confluencia,
ámbar para espera, rojo moderado para descarte; significado operativo.
Depth: bordes suaves y separación por espacio, siguiendo el panel existente.
Surfaces: fondo papel y un nivel blanco para análisis; sin modo oscuro.
Typography: Segoe UI y cifras tabulares; peso distingue señal de metadatos.
Spacing: unidad de cuatro píxeles, filas compactas y secciones separadas.
Signature: decisión → evidencia → condiciones pendientes → niveles de referencia,
junto a cobertura explícita de spot/futuros/DEX y dirección verificable del contrato.

Los accesos de monedas fijas reutilizan botones nativos con 40px de altura y
tokens existentes. La checklist prioriza requisitos pendientes y cifras observadas;
los escenarios long/short usan disclosures nativos independientes, conservando
su apertura durante las actualizaciones. Se mantienen paleta, tipografía,
superficies y ritmo de cuatro píxeles para que revisar condiciones no implique
aprender una interfaz nueva.

Se evita un muro de tarjetas idénticas: lista comparativa, detalle de evidencia y
bitácora de fuentes. No hay controles de compra, balances ni operaciones nuevas.
El bloqueo de `run` está implementado en código y también se aplica a Docker y
otros directorios de trabajo. `ANALYSIS_ONLY` registra la política; borrarlo no
habilita operaciones.
