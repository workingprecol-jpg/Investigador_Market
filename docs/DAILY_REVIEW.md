# Revisión diaria y mejora de estrategias

El equipo incluye un coordinador y seis especialistas. El sexto, **Investigación y validación**, analiza el historial y somete las propuestas a una comparación con datos nuevos. Es un módulo del motor Python; la mejora consiste en ajustes pequeños de parámetros dentro de reglas programadas, no en reescribir su propio código ni entrenar libremente un modelo después de cada operación.

## Cuándo revisa

La instancia continua realiza la revisión a partir de las **00:10 de Colombia (UTC−5)** para el día que terminó a las 00:00. Si el PC estuvo apagado, procesa los días completos pendientes desde que existe el historial, empezando por el más antiguo. El corte temporal excluye operaciones posteriores al cierre de ese día. Conserva la fecha revisada para evitar duplicar una revisión aplicada.

La revisión presenta el día cerrado, los últimos 7 días, los últimos 30 días y el historial completo registrado hasta el corte. La pausa preventiva diaria del motor utiliza su propia jornada UTC; no debe confundirse con el horario colombiano del informe. La gestión de stops, salidas, conciliación y límites continúa durante los ciclos habituales.

## Qué revisa cada especialista

| Especialista | Evidencia que revisa | Tipo de propuesta |
|---|---|---|
| Tendencia | Resultados por régimen y condiciones de entrada | Filtros de elegibilidad más selectivos |
| Indicadores | Momentum, volatilidad y entradas que terminaron perdiendo | Umbral mínimo de momentum más selectivo |
| Liquidez | Costes, spread y condiciones de ejecución registradas | Restringir entradas costosas |
| Noticias y contexto | Vigencia y calidad del contexto de cada decisión | Exigir evidencia de contexto suficiente |
| Riesgo y rendimiento | Resultado neto, exposición y pérdidas | Reducción conservadora del tamaño |
| Investigación y validación | Muestra, atribución, propuestas y resultados de referencia/candidata | Mantener, investigar, validar o descartar |

Las operaciones nuevas enlazan con la decisión que originó la entrada y sus datos de mercado. Los registros antiguos siguen formando parte del resultado económico; si les falta ese enlace, no se les atribuye una señal o un régimen inventado. Tampoco se afirma que una oportunidad vetada habría generado un beneficio: los resultados de las carteras paralelas se identifican como simulados.

Los informes incluyen resultado neto, comisiones, financiación, expectativa, profit factor y número de posiciones con cierre verificable. Separan resultados por moneda, dirección y régimen cuando existen metadatos suficientes. El porcentaje de aciertos no es el criterio único para mejorar.

Desde la incorporación del registro `news_articles`, las decisiones nuevas
guardan IDs de titulares vinculados al símbolo o al veto explícito. La revisión
existente todavía compara principalmente `fresh` y el resultado de operaciones
enlazadas; **no demuestra causalidad de una noticia**. Cualquier hipótesis nueva
basada en eventos debe usar la hora de primera observación y compararse con una
cartera de referencia en datos posteriores. Véase [NEWS_DECISIONS.md](NEWS_DECISIONS.md).

## De propuesta a cambio

1. **Hipótesis auditable.** La revisión necesita al menos 30 posiciones cerradas verificables para proponer un ajuste. Los filtros técnicos y de contexto necesitan además suficientes operaciones enlazadas con los datos relevantes. Cada propuesta explica qué campo cambiaría y por qué. Ausencia de evidencia produce `insufficient_data` o `no_change`.
2. **Un campo por experimento.** Se cambia un parámetro permitido, con un valor acotado y comparado con la configuración original. Los límites de capital, pérdida, modo de ejecución, apalancamiento y credenciales quedan fuera de la optimización.
3. **Comparación prospectiva.** La estrategia de referencia y la candidata operan en carteras paper paralelas usando las siguientes observaciones del mercado. No generan órdenes adicionales en Binance Demo. Se contabilizan costes de simulación y se preservan las protecciones de riesgo.
4. **Muestra y comparación.** Se exigen al menos 7 días transcurridos y 7 días de cobertura efectiva observada, además de 30 cierres en cada cartera. La candidata necesita resultado realizado y equidad neta de liquidación positivos, profit factor de al menos 1,2 —o ganancias y ninguna pérdida—, beneficio neto y expectativa por riesgo inicial no inferiores a la referencia, y caída máxima no superior. Ninguna cartera puede haber incumplido sus límites. Se necesitan precios recientes de todos los símbolos y la misma configuración de referencia. Un backtest que explique bien el pasado no basta para aprobar un cambio.
5. **Aplicación controlada.** El coordinador solo incorpora un ajuste aprobado cuando no hay posiciones ni órdenes pendientes y la configuración/versión de referencia sigue coincidiendo. Registra versión, campo, evidencia y decisión. Las entradas posteriores usan la versión nueva.

La adaptación conservadora existente, que reduce el riesgo ante bloques no rentables de 30 operaciones, se calcula ahora durante la revisión diaria. Esta reducción preventiva es distinta de aprobar un cambio de estrategia a partir de un experimento. Nunca aumenta el riesgo para recuperar pérdidas.

El validador puede rechazar una candidata que siga fallando los criterios después de 14 días con muestra y cobertura suficientes. Si no hay muestra, continúa observando; los experimentos caducan a los 30 días. Apagar el PC no acumula cobertura válida. Una cartera paper reproduce bid/ask, comisiones, deslizamiento y financiación estimados: sus ejecuciones no son fills reales de Binance.

Un día rentable no obliga a cambiar reglas, ni un día perdedor demuestra que una regla deba descartarse. Evaluar todos los días no implica modificar todos los días. Los controles son criterios operativos; no constituyen una prueba de ganancias futuras.

## Consultar y ejecutar

Desde `D:\BotTradingFinalBoss`:

```powershell
python -m bot daily-review --mode paper --preview
python -m bot daily-review --mode paper --date 2026-09-06 --preview
python -m bot report --mode paper --output reports/latest.html
```

`--preview` calcula una vista sin modificar el estado de trading, iniciar experimentos ni aplicar propuestas. Sin fecha, muestra el día actual incompleto; `--date` permite elegir el día del informe. Guarda el resultado con sufijo `-preview.json`. Para generar una revisión del día anterior, ejecuta `python -m bot daily-review --mode paper`; el motor continuo recogerá la revisión completa y decidirá las siguientes acciones. No hace falta detener la gestión de posiciones: el cálculo usa un lector de SQLite y una base separada para las revisiones, con bloqueo para evitar dos revisores simultáneos. Normalmente basta dejar el motor continuo ejecutándose: ya contiene el calendario de revisión. Para el historial de Binance Demo usa `--mode demo`.

Los informes diarios se guardan en `reports/paper/reviews/` o `reports/demo/reviews/`, y su registro está en `data/paper.reviews.sqlite3` o `data/demo.reviews.sqlite3`. El cálculo se ejecuta en un subproceso oculto con tiempo máximo; su estado y errores se publican en `daily_schedule`.

Para volver manualmente a la versión anterior, detén la instancia y verifica que no queden posiciones ni órdenes pendientes antes de ejecutar `python -m bot rollback --mode paper`. El comando registra la reversión y conserva la contabilidad; reinicia la instancia para cargarla. No recupera dinero perdido ni borra el historial. Para detener o reanudar la ejecución sigue el [README](../README.md).

El panel y el JSON muestran la revisión, los seis especialistas, la estrategia activa, los experimentos, la contabilidad y las decisiones auditadas. Una aprobación de señal no acredita que se ejecutara la orden; un experimento pendiente no acredita una mejora. La curva utiliza únicamente observaciones de equidad guardadas desde que se habilitó ese registro.

## Grok Bot

Los siete roles de la aplicación pueden supervisar esta información con acceso explícito a los informes. El motor no convierte mensajes de Grok en órdenes ni aplica texto libre como configuración. Los prompts y la distribución de responsabilidades están en [GROK.md](GROK.md). La aplicación aún necesita su conexión y configuración manual; modificar este proyecto no crea bots dentro de Grok.
