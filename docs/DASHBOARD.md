# Panel de operaciones Final Boss

**Panel actual: radar de oportunidades de solo análisis.** En el mismo puerto
8765, `python -m bot scan-dashboard` sirve `bot/scanner.html` con resultados del
buscador público `scan`. No muestra nuevas operaciones ni coloca órdenes.
Las tareas actuales son `FinalBoss-scanner` y `FinalBoss-dashboard-analysis`.
Consulta [SCANNER.md](SCANNER.md). El resto de este documento describe el panel
histórico de trading, cuyas tareas están deshabilitadas.

El nuevo panel presenta el coordinador, sus seis especialistas y la cuenta de simulación en una interfaz **exclusivamente clara**. Es una aplicación de lectura: no incorpora controles para colocar órdenes, modificar límites ni pausar el motor.

El panel se sirve desde el proceso local del proyecto en `http://127.0.0.1:8765/`. La página necesita ese servidor para consultar su API; abrir `bot/dashboard.html` como archivo no conecta con el motor. Los informes estáticos de `reports/paper.html` y `reports/demo.html` siguen siendo exportaciones separadas.

## Inicio en Windows

Las tareas `FinalBoss-paper` y `FinalBoss-dashboard-paper` ejecutan el motor y el panel por separado, sin ventana, al iniciar sesión. El PC debe permanecer encendido, conectado y sin suspensión. Para iniciar las tareas ya instaladas:

```powershell
Start-ScheduledTask -TaskName 'FinalBoss-paper'
Start-ScheduledTask -TaskName 'FinalBoss-dashboard-paper'
```

Para instalar o actualizar únicamente la tarea del panel, desde la carpeta del proyecto:

```powershell
.\scripts\install-dashboard.ps1 -Mode paper -Start
```

El puerto predeterminado es 8765 para paper y 8766 para demo; `-Port` permite elegir otro. También puede ejecutarse en una terminal mediante `python -m bot dashboard --mode paper`. Usa una sola instancia por puerto. La API `/health` confirma que el servidor responde; el estado de continuidad del panel confirma por separado si el motor produce ciclos recientes. Una pausa de riesgo registrada continúa bloqueando entradas aunque ambos procesos estén activos.

## Qué muestra

- **Cartera:** equidad, resultado neto, caída desde máximo y cierres registrados. Los porcentajes de acierto y profit factor muestran ausencia de muestra cuando corresponde.
- **Mercado:** DOGE, SOL, ETH y `1000PEPEUSDT`. Seleccionar un contrato cambia el gráfico de velas. La pestaña Equidad muestra observaciones de la cartera. La variación del selector corresponde al tramo de velas disponible; no se presenta como variación de 24 horas.
- **Equipo:** tarea, estado, contrato, resultado, hora y próxima ejecución cuando está registrada. El coordinador tiene un espacio propio, seguido de tendencia, indicadores, liquidez, noticias y contexto, riesgo e investigación y validación. La evidencia adicional se abre desde cada especialista.
- **Bitácora:** eventos reales del motor, filtrables por agente. El evento documenta lo ocurrido a esa hora; una ejecución breve puede aparecer como completada entre dos consultas.
- **Posiciones:** entrada, precio de marca disponible, stop, objetivo, cantidad y riesgo. El PnL abierto de la tabla es el movimiento de precio desde la entrada; no descuenta comisiones ni financiación. Una marca antigua queda identificada.
- **Historial:** cierres y decisiones, con filtro por contrato. El resultado neto de cierre incluye los costes contabilizados; una señal aprobada no confirma por sí sola la ejecución.
- **Noticias y compra:** estado y hora de la última consulta RSS, titulares con vínculo al contrato seleccionado y `COMPRAR` / `ESPERAR` / `NO_COMPRAR` de la última vela. Una señal `COMPRAR` se debe leer junto con el estado de ejecución; el panel no coloca órdenes. Si el ciclo envejece, muestra `ESPERAR`.
- **Investigación:** última revisión diaria, siguiente horario, métricas por periodo, conclusiones, propuestas y comparación entre referencias y candidatas.

Las horas se muestran en Colombia (UTC−5). PAPER identifica la simulación local; DEMO identifica órdenes con fondos virtuales en Binance. Los módulos son los implementados por el motor, no agentes que inventan pensamientos o conversaciones para llenar la interfaz.

## Actualización y datos antiguos

La página solicita `GET /api/dashboard` cada dos segundos. No hay servicios externos, fuentes descargadas, dependencias de CDN ni envío de datos desde esta interfaz a otro dominio.

La casilla **Auto · 2 s** pausa únicamente las consultas del panel. El motor sigue funcionando. La preferencia se conserva en la sesión del navegador si su almacenamiento está disponible; **Actualizar** permite obtener una lectura manual aun con la actualización automática pausada.

La continuidad se calcula desde `last_cycle`, no desde la hora en que el servidor responde. Pasados 180 segundos sin un ciclo reciente se muestra el aviso correspondiente. La condición “En curso” exige una tarea realmente registrada, telemetría reciente y un ciclo reciente. Si la conexión falla, la respuesta es inválida o se pausa la actualización, las cifras anteriores se conservan y los estados en curso pierden su indicación de actividad actual.

El mercado conserva sus propias fechas de captura y antigüedad de velas y marcas. Que el panel responda no vuelve recientes los precios. Investigación permanece en espera hasta que exista una ejecución registrada; el horario previsto no se representa como una tarea ejecutándose.

La sección de noticias lee exclusivamente el estado que publicó el motor; el
navegador no consulta servicios externos. Detalles de cobertura y límites:
[NEWS_DECISIONS.md](NEWS_DECISIONS.md).

## Verificación de la interfaz

Desde la carpeta del proyecto:

```powershell
node --test tests/test_dashboard_browser.js
```

Las pruebas ejecutan el JavaScript real mediante el entorno de Node, con DOM y respuestas simuladas. Comprueban la antigüedad, los estados de actividad, la retención de datos ante desconexión, las respuestas inválidas, los filtros, los textos no confiables y la ausencia de acciones de trading. La revisión visual de escritorio y móvil se realiza sobre el servidor local, no sobre una copia con datos inventados.

La dirección visual y la referencia de Interface Design están documentadas en `docs/design/DIRECTION.md` y `docs/design/interface-design/`.
