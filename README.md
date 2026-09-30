# Final Boss · radar de oportunidades (solo análisis)

**Operación actual: información y señales para decidir manualmente. No ejecuta
trades paper, Demo ni reales.** La política registrada en `ANALYSIS_ONLY` bloquea
el motor heredado en código. Se conserva su historial y las tareas antiguas quedan deshabilitadas.

El buscador mantiene seguimiento fijo de BTC, ETH (Ethereum), SOL, BNB, XRP,
ADA, DOGE, AVAX, LINK, DOT, LTC, TRX, SUI, TON y PEPE. Revisa pares spot USDT
y contratos perpetuos USDT de Binance con velas 15m/1h, además de líderes y pares
en rotación. También investiga perfiles DEX y consulta noticias CoinDesk.
El panel distingue `Spot condicionado`, `Long condicionado`, `Short condicionado`,
`Esperar`, `Investigar DEX` y `Descartar`. Cada escenario muestra requisitos,
valores observados, condiciones cumplidas y pendientes, e invalidación y objetivo.
Los futuros usan datos de su propio contrato y funding; no se deducen del spot.
No garantiza seguridad de entrada, subidas ni rentabilidad.

Incluye gráficos de velas 15m/1h/4h/1d con EMA/VWAP y volumen, open interest,
flujo comprador/vendedor y funding, riesgos del token DEX, ranking frente a BTC
y por aceleración de volumen, grandes transacciones BTC e historial observado.
Atribución de ballenas, valoraciones de monedas principales y calendario de
desbloqueos tienen conexiones opcionales mediante claves del entorno.
Detalles y límites en [RESEARCH.md](docs/RESEARCH.md).

Perfil del auditor financiero: [analista-financiero.md](agents/analista-financiero.md),
con instrucciones XML para revisar cálculos, riesgos, datos, interfaz y operación.
Se activa manualmente pidiendo leer el archivo y ejecutar su auditoría; genera
hallazgos y mejoras con evidencia, sin trades ni modificaciones operativas.

```powershell
python -m bot scan --once
python -m bot scan
python -m bot scan-dashboard --port 8765
python -m bot scan-status
# Arranque oculto al iniciar sesion, con dos tareas independientes:
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-scanner.ps1 -Start
```

Panel: **http://127.0.0.1:8765/**. Detalles y criterios en [SCANNER.md](docs/SCANNER.md).
Las instrucciones siguientes describen el motor histórico y no autorizan
reactivar ejecución de órdenes; sus comandos `run` están bloqueados.

## Referencia del motor histórico

Motor Python de futuros USDⓈ-M con un coordinador y **seis especialistas**, revisión diaria y validación prospectiva de cambios. Configurado para **100 USDT virtuales, apalancamiento 5x, margen aislado, posiciones long y short**, y contratos **DOGEUSDT, SOLUSDT, ETHUSDT y 1000PEPEUSDT**.

El objetivo es encontrar y validar una estrategia con beneficio neto. **No existe garantía de ganancias**: el sistema puede perder y una prueba Demo rentable no demuestra rentabilidad real. Esta versión no incorpora trading con dinero real. Los especialistas del motor son módulos deterministas y un lector de noticias; no son seis llamadas permanentes a un LLM. La organización equivalente dentro de la aplicación Grok Bot se explica en [docs/GROK.md](docs/GROK.md).

## Equipo y decisiones

| Miembro | Trabajo implementado |
|---|---|
| Coordinador | Recoge informes, aplica vetos, entra una vez por vela y conserva estado/órdenes |
| 1. Tendencia | Régimen alcista, bajista o lateral; EMA20/50/200 y pendiente |
| 2. Indicadores | RSI14, MACD12/26/9, ATR14; dirección y volatilidad |
| 3. Liquidez | Spread, volumen de 24 horas y condiciones de ejecución |
| 4. Contexto | Noticias RSS recientes, fechas y fuentes; detección limitada de incidentes; Grok API opcional |
| 5. Riesgo y rendimiento | Tamaño, margen, límites, exposición agregada y adaptación conservadora |
| 6. Investigación y validación | Revisión diaria del historial, evaluación por especialista, propuestas acotadas y comparación prospectiva |

Analiza velas cerradas de 5 minutos; revisa posiciones y mercado cada 30 segundos más la duración de las consultas. La puntuación combina tendencia (65%) e indicadores (35%). Exige acuerdo de dirección, umbral mínimo y ausencia de vetos. Contexto aporta avisos/vetos, sin capacidad de elevar el riesgo ni ejecutar instrucciones recibidas en noticias.

El lector RSS se actualiza independientemente del cierre de velas y registra en
SQLite cada versión de titular con su primera observación y símbolos mencionados.
Un fallo de la fuente bloquea entradas nuevas si el monitoreo está activado;
`require_fresh_news = true` exige además un titular reciente. El informe y el
panel muestran `COMPRAR`, `ESPERAR` o `NO_COMPRAR` por símbolo junto con el estado
de ejecución: una señal `COMPRAR` no demuestra que se haya llenado una orden.
CoinDesk sigue siendo una única fuente editorial y los titulares no constituyen
confirmación independiente. Consulta [Noticias y decisiones](docs/NEWS_DECISIONS.md).

El coordinador conserva un registro de aprobaciones, descartes y vetos; los cierres nuevos enlazan con su decisión de entrada para poder estudiar sus condiciones. A las **00:10 de Colombia (UTC−5)** evalúa el día terminado, los últimos 7 y 30 días y el historial completo. Cada especialista recibe una evaluación y puede proponer un cambio; mantener la estrategia es una decisión válida. El sexto especialista separa la hipótesis de la validación. Consulta el procedimiento y sus límites en [docs/DAILY_REVIEW.md](docs/DAILY_REVIEW.md).

## Capital, pérdidas y profit

| Regla inicial | Configuración |
|---|---|
| Capital reservado | 100 USDT, aunque la cuenta Demo tenga más saldo |
| Apalancamiento | 5x; se rechaza el modo hedge y se usa one-way + margen aislado |
| Pérdida total máxima solicitada | 30 USDT (30% del capital inicial) |
| Pausa diaria preventiva | 5% de la equidad al inicio del día UTC, inicialmente 5 USDT |
| Pausa por caída desde máximo | 10% de la equidad máxima; más estricta que el límite total |
| Riesgo inicial por entrada | Hasta 0,5% de equidad, aproximadamente 0,50 USDT inicialmente |
| Margen por posición | Máximo 10% de equidad; con 5x, hasta 50 USDT de nocional inicialmente |
| Posiciones simultáneas | Máximo 2, con presupuesto de pérdida agregado |
| Stop loss | 2 × ATR, mínimo 0,3% del precio; se descarta un stop mayor de 8% |
| Take profit | Movimiento favorable de 2 veces la distancia del stop (2R bruto) |
| Costes paper | Comisión estimada 0,05% por lado, deslizamiento 5 puntos básicos por lado, funding estimado |

El 30% es un límite de pérdida, no un presupuesto que el bot intente consumir. Los límites se comprueban sobre equidad incluyendo posiciones abiertas. Las pausas ordenan cerrar las posiciones registradas y bloquean nuevas entradas. Gaps, retrasos o caídas de red pueden hacer que la pérdida efectiva supere un umbral; un stop no garantiza precio de ejecución.

El take profit expresa un objetivo por operación, **no 5% de rentabilidad diaria**. La rentabilidad se informa neta de costes. La adaptación conservadora por bloques independientes de 30 operaciones se evalúa durante la revisión diaria: si el bloque no es rentable, reduce a la mitad el tamaño, hasta un suelo de 25% del riesgo inicial. No cambia los parámetros después de cada trade. Los ajustes de estrategia requieren validación prospectiva antes de aplicarse; no aumentan apalancamiento ni relajan los límites originales y no reescriben código.

Binance impone lotes y nocionales mínimos. Si el tamaño permitido no los cumple, omite la entrada; nunca redondea al alza. `1000PEPEUSDT` cotiza un contrato referido a 1.000 PEPE; se usan sus precios y cantidades directamente.

## Inicio en Windows

Requiere **Python 3.11 o posterior**. Solo usa biblioteca estándar; no hay dependencias que instalar.

```powershell
cd D:\BotTradingFinalBoss
Copy-Item -LiteralPath .env.example -Destination .env
python -m unittest discover -v
python -m bot probe
python -m bot run --mode paper --once
python -m bot run --mode paper
```

Para comparar los perfiles paper aislados de tendencia, swing y scalping sin
mezclar capital ni resultados, consulta [docs/STRATEGY_PROFILES.md](docs/STRATEGY_PROFILES.md).

No vuelvas a copiar `.env.example` sobre un `.env` que ya contiene tus claves.

- **paper:** simula fills localmente con cotizaciones públicas de Binance Demo. No crea órdenes en tu cuenta y no necesita claves.
- **demo:** envía órdenes virtuales reales a la API de Binance Futures Demo. Necesita claves exclusivas de ese entorno.

Los dos modos tienen historiales distintos: `data/paper.sqlite3` y `data/demo.sqlite3`. El modo predeterminado es `paper`. No se cambia automáticamente a Demo ni a dinero real.

## Activar las pruebas en tu cuenta Binance Demo

1. Entra en [Binance Demo](https://demo.binance.com/) y crea una API key desde su administración de API. Usa permisos de futuros/trading Demo. El código utiliza claves HMAC (API key y secret).
2. Utiliza una cuenta Demo dedicada sin posiciones, órdenes ni operaciones manuales. Mantén disponible al menos 100 USDT virtuales y modo de posición **one-way**. El bot configura margen aislado y 5x al abrir el primer trade de cada contrato.
3. Abre `.env` en un editor local y completa `BINANCE_DEMO_API_KEY` y `BINANCE_DEMO_API_SECRET`. **No pegues las claves en chats, informes o Git.**
4. Prueba un ciclo con `python -m bot run --mode demo --once`. Ese comando puede abrir una posición Demo si aparece una señal válida; no es solo una prueba de credenciales.
5. Revisa `python -m bot status --mode demo` y arranca `python -m bot run --mode demo` o el script de Windows indicado abajo.

El único host privado admitido está fijado en código: `https://demo-fapi.binance.com`. No se puede sustituir por producción mediante configuración. Se usa la API condicional actual `/fapi/v1/algoOrder` para el stop `STOP_MARKET`, `closePosition=true` y precio de marca. [Documentación oficial de futuros](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info), [órdenes condicionales](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order).

El **stop queda alojado en Binance Demo**. El **take profit y la salida por tiempo se ejecutan desde el motor**, por lo que necesitan que el proceso y la conexión funcionen. Si no puede confirmar la protección, intenta cerrar la posición y se detiene para revisión. Las órdenes inciertas se registran antes de enviar y se consultan por su identificador; nunca se reenvían a ciegas.

## Operación continua en este PC

Para iniciar un proceso oculto:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-windows.ps1 -Mode paper
python -m bot health --mode paper
```

Para instalar arranque automático al iniciar tu sesión:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-startup.ps1 -Mode paper
```

El instalador crea o actualiza la tarea `FinalBoss-paper` del Programador de tareas, con reinicio al fallar. La tarea ejecuta directamente el `pythonw.exe` del Python instalado, sin una consola ni un PowerShell intermediario; guarda rutas absolutas del ejecutable, proyecto y configuración. Así el Programador supervisa el proceso de trading. Requiere `pythonw.exe`; el inicio manual con `start-windows.ps1` admite también `python.exe` oculto si no está disponible. Los logs permanecen en `data/paper.log`.

Para revisar la acción sin instalar ni iniciar el bot, añade `-Preview` a cualquiera de los dos scripts. Después de instalar, puedes iniciar la tarea con `Start-ScheduledTask -TaskName FinalBoss-paper` si no existe ya una instancia manual. Usa `-Mode demo` cuando hayas completado las claves y quieras operar dentro de la cuenta Demo. El bloqueo del archivo de estado impide dos escritores para la misma instancia. El PC debe permanecer **encendido, conectado y sin suspensión**. El arranque configurado es al iniciar sesión, no antes del login. No se modifican automáticamente las políticas de energía.

Consultar:

```powershell
python -m bot status --mode paper
python -m bot report --mode paper --output reports/latest.html
python -m bot report --mode paper --output reports/latest.json
Get-Content -LiteralPath data/paper.log -Tail 20
```

El dashboard de agentes en vivo está en **http://127.0.0.1:8765/** para paper. Es una aplicación local de solo lectura, con tema claro, datos cada dos segundos, actividad real de los siete módulos, velas, posiciones e investigación diaria. Su servicio se mantiene separado del motor para poder mostrar una interrupción de datos. Consulta [DASHBOARD.md](docs/DASHBOARD.md) para instalación, tareas de Windows y funcionamiento.

Los informes exportados siguen en **`reports/paper.html`** y **`reports/paper.json`**; en Demo usan el nombre `demo`. El motor los publica después de cada ciclo correcto. Las exportaciones como `reports/latest.html` son copias: el motor no las actualiza automáticamente.

El panel recarga el archivo cada 30 segundos, con una casilla para pausar y el botón **Actualizar ahora**. Conserva la preferencia de pausa en la sesión del navegador cuando este lo permite. El contador calcula cada segundo la antigüedad real del último ciclo guardado; muestra **Datos recientes** hasta 180 segundos y **Sin actualización reciente** después. Recargar un archivo antiguo no vuelve reciente su información. Estas etiquetas indican la fecha de los datos, no confirman que el proceso siga vivo. Si JavaScript está desactivado, usa F5; el contador será el calculado al generar el informe.

Las horas principales están en Colombia (UTC−5); las tablas marcadas UTC conservan esa zona. **PAPER** significa simulación local, sin órdenes en la cuenta de Binance; **DEMO** significa órdenes virtuales en Binance. El panel incluye curva de equidad observada, seis especialistas, revisión diaria, versión activa, experimentos, aprobaciones y vetos, costes y operaciones. Presenta campos sin muestra como pendientes; la salud del proceso no acredita rentabilidad de la estrategia. Son archivos locales sin servidor expuesto. `health` devuelve código 1 si el estado está detenido, es antiguo o hubo un error. Los logs rotan y SQLite registra análisis, intentos, ejecuciones, financiación y ajustes.

Para consultar una evaluación manual sin modificar el estado:

```powershell
python -m bot daily-review --mode paper --preview
python -m bot daily-review --mode paper --date 2026-09-06 --preview
```

La evaluación automática forma parte del proceso continuo; no requiere una segunda tarea de Windows. La revisión y la validación paralela no sustituyen la gestión continua de posiciones.

### Parar y reanudar

```powershell
python -m bot stop --mode paper
```

Crea `data/paper.STOP`. El proceso intentará cerrar posiciones y pausará entradas en el próximo ciclo con conexión. **Consulta `python -m bot status --mode paper` y verifica que `positions` esté vacío y `pending_orders` sea 0 antes de apagarlo.** `Ctrl+C` termina el proceso; no equivale a liquidar posiciones. En Demo, las protecciones aceptadas permanecen en Binance.

Una vez comprobado el cierre, termina una tarea instalada con `Stop-ScheduledTask -TaskName FinalBoss-paper`. Este comando detiene el proceso; no solicita liquidar posiciones ni cancela su arranque en el próximo inicio de sesión. Para impedir también nuevos arranques, usa `Disable-ScheduledTask -TaskName FinalBoss-paper` y vuelve a habilitarla cuando corresponda. Si se inició manualmente con `start-windows.ps1`, el PID está en `data/paper.pid`; comprueba que ese PID sigue correspondiendo al bot y su configuración antes de terminarlo, porque Windows puede reutilizar identificadores.

Las tareas antiguas que ejecutaban `run-worker.ps1` mediante PowerShell pueden dejar vivo el Python hijo al usar `Stop-ScheduledTask`. Si migras una instalación antigua, verifica también los procesos Python y detén únicamente la instancia identificada de este proyecto. Después vuelve a ejecutar `install-startup.ps1` para registrar la acción directa. Editar los scripts por sí solo no cambia una tarea ya registrada.

Cuando el proceso esté detenido y no haya posiciones/órdenes pendientes, `python -m bot resume --mode paper` elimina la pausa manual. Reinicia mediante la tarea programada o el script manual, según cómo la operes; no ambos. No borres SQLite para ocultar pérdidas ni reanudes tras un error de conciliación sin comparar la cuenta Demo. Cambiar capital, símbolos o modo exige un estado independiente y reconciliado.

## Alternativa de despliegue posterior

Incluye Docker para un servidor con almacenamiento persistente:

```powershell
docker compose up -d --build
docker compose logs --tail 30 trader
docker compose exec trader python -m bot status --mode paper
```

`BOT_MODE=demo` selecciona Demo para Compose; las claves se leen desde `.env`. Los datos e informes usan volúmenes persistentes. No ejecutes `docker compose down -v` si necesitas preservar el historial. `restart: unless-stopped` recupera un proceso que termina; un healthcheck fallido por sí solo no reinicia el contenedor. Requiere Docker Engine activo; no sustituye la disponibilidad del PC/servidor.

## Evaluación antes de considerar dinero real

El informe contiene beneficio neto, operaciones, porcentaje de aciertos, profit factor, expectativa, caída desde máximo, posiciones y bloqueos. Cero operaciones significa que todavía no hay evidencia, no un bot rentable.

Para una investigación histórica inicial, ejecuta `python -m bot research`. Guarda `reports/research.json`: compara nueve combinaciones de umbral/ATR en el primer 60% de las velas y evalúa solo la elegida en el 40% restante, entrando en la apertura posterior a la señal. Cada activo/tramo usa 100 USDT independientes; no se suman como cartera. El historial descargado es corto y no reconstruye noticias, spreads ni funding históricos; usa costes aproximados explícitos. No cambia la configuración del motor ni aprueba trading real.

Recoge diferentes regímenes de mercado y una muestra suficiente (como punto de partida, 100 operaciones cerradas y varias semanas). Separa desarrollo y validación cronológicamente; revisa también resultados por activo, rachas de pérdidas, costes y comportamiento tras desconexiones. Las noticias RSS y la liquidez Demo son aproximaciones; el funding de `paper` usa la última tasa observada, mientras `demo` concilia ingresos reales de la cuenta virtual. No se ha demostrado una ventaja estadística ni está implementada una promoción automática a dinero real.

La guía de [Grok Bot](docs/GROK.md) incluye instrucciones copiables para el coordinador y los seis especialistas de la app. Es una integración de supervisión mediante acceso a código e informes; este repositorio no crea una sesión ni un conector no documentado dentro de Grok Bot.
