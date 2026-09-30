# Perfiles paper aislados

Los perfiles nuevos no comparten capital, posiciones, base SQLite, bloqueo,
telemetría, log ni informe. La suma del capital virtual es 100 USDT:

| Perfil | Capital | Intervalo | Señal | Riesgo por operación |
|---|---:|---:|---|---:|
| trend | 40 USDT | 15m | EMA20/50/200 + RSI/MACD | 0,25% |
| swing | 40 USDT | 1h | tendencia EMA + retroceso moderado | 0,25% |
| scalping | 20 USDT | 1m | EMA9/21/50 + RSI7/MACD5/13/4 | 0,25% |

Los tres perfiles operan `SOLUSDT`, `ETHUSDT` y `1000PEPEUSDT`, con una sola
posición máxima. Empiezan solo en largo porque la muestra verificada anterior
tuvo 0 ganadoras en 15 posiciones bajistas. DOGE queda fuera por ser el mayor
contribuyente histórico a las pérdidas.

Inicio continuo:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-paper-strategies.ps1
```

Estado individual:

```powershell
python -m bot status --mode paper --config configs/trend.toml --state data/trend.sqlite3
python -m bot status --mode paper --config configs/swing.toml --state data/swing.sqlite3
python -m bot status --mode paper --config configs/scalping.toml --state data/scalping.sqlite3
```

Los informes continuos se publican como `reports/trend.html`,
`reports/swing.html` y `reports/scalping.html`. No se debe considerar una
estrategia candidata para Demo hasta reunir como mínimo 100 cierres totales,
30 cierres fuera de muestra, PnL neto positivo, profit factor superior a 1,2 y
drawdown dentro de su límite.

Los tres perfiles tienen `news_enabled = false`: sus decisiones de compra se
basan en señales técnicas, liquidez y riesgo, sin cobertura RSS. Para ensayar
noticias con ellos hay que crear estados paper nuevos y comparar prospectivamente;
cambiar estos TOML sobre una base existente alteraría su huella de configuración.
Consulta [NEWS_DECISIONS.md](NEWS_DECISIONS.md).
