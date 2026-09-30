# Noticias y decisión de compra

## Alcance implementado

El motor sigue operando exclusivamente en `paper` o Binance Demo. El lector de
`bot/context.py` consulta el RSS público de CoinDesk cada
`news_refresh_seconds` (900 segundos por defecto), independientemente de que
haya cerrado una vela nueva. El límite de respuesta, la lista de destinos HTTPS,
la validación de fechas y los límites de titulares siguen activos.

Cada versión de un titular tiene un ID estable calculado sobre URL, texto y fecha
de publicación. `news_articles` en la misma base SQLite registra fuente, URL,
texto, publicación, primera y última observación, y símbolos mencionados. Una
corrección del texto crea otro ID, de modo que una revisión posterior no reescribe
lo que el motor vio inicialmente. La tabla sirve para estudiar cobertura y hacer
reproducciones cronológicas; no se deben atribuir a una entrada noticias recibidas
después de su hora de decisión.

El vínculo con DOGE, SOL, ETH o PEPE se obtiene de palabras completas del
título. Es un filtro de relevancia limitado, no verificación del contenido ni
medición de su impacto. Un titular general no se presenta como noticia específica
del contrato. Si Grok está habilitado, solo clasifica titulares vinculados al
símbolo y no decide órdenes. El veto local de incidentes explícitos de Binance
continúa siendo deliberadamente estrecho.

La última consulta se expone en `news.status`, `source_ok`, `last_attempt_at` y
`fetched_at` del JSON de informe. Un fallo de la fuente invalida de inmediato la
vigencia aunque haya titulares anteriores en memoria, y bloquea **nuevas entradas**
cuando el monitoreo de noticias está activado. Las posiciones abiertas siguen
gestionándose con sus stops y reglas existentes. `require_fresh_news = true` añade
la exigencia de al menos un titular reciente; puede reducir mucho la actividad
cuando el feed publica poco. No equivale a exigir una noticia relevante para cada
activo. Los perfiles con `news_enabled = false` siguen sin consultar noticias.

## Cómo leer COMPRAR, ESPERAR y NO_COMPRAR

La decisión se guarda por símbolo y vela en `last_analysis.buy_decision`, junto
con `execution.status`, motivo y `news_evidence_ids`. `COMPRAR` indica una señal
long elegible o una entrada long registrada; hay que leer `execution.status` para
saber si hubo un fill. `ESPERAR` indica una pausa, veto de contexto, datos de
mercado/contrato pendientes o una orden todavía sin confirmar. `NO_COMPRAR`
indica que la señal no autoriza una compra; una señal short puede tener su propia
dirección en el registro. Ninguna etiqueta estima la probabilidad de beneficio.

El panel local y los informes muestran la fuente, el momento de consulta, los
titulares recientes y si mencionan al contrato seleccionado. Si el ciclo del
motor deja de ser reciente, el motor no está sano o falla la fuente de noticias
activa, el panel presenta `ESPERAR` aunque la última vela guardada tuviera una
señal de compra. Un titular por sí solo nunca incrementa la puntuación técnica
ni el presupuesto de riesgo.

## Límites y siguiente validación

El RSS de CoinDesk sigue siendo **una sola fuente editorial** y puede omitir
incidentes, correcciones o noticias importantes. Binance publica anuncios
oficiales mediante un WebSocket que requiere una API key de Binance y un proceso
de autenticación, ping y reconexión. Esta instalación solo dispone del flujo
Demo documentado: no se ha conectado ese WebSocket ni se han solicitado claves
adicionales. No se usan interfaces de anuncios no documentadas.

`exchangeInfo` de Binance Demo ya comprueba que el contrato sea perpetuo USDT y
tenga estado `TRADING` antes de normalizar una orden. Un fallo de esa comprobación
se registra ahora como entrada omitida. El calendario oficial de publicaciones
macroeconómicas puede añadirse en una etapa separada, después de validar su
zona horaria y la política de ventanas de espera.

Antes de atribuir una mejora a las noticias, comparar prospectivamente una
referencia y una candidata con los mismos precios y costes, usando solo
titulares observados antes de cada decisión. Medir resultados por símbolo,
periodo, cobertura de fuente y tipo de evento. Los datos históricos descargados
por `research` no reconstruyen noticias pasadas y no sirven para aprobar este
filtro.
