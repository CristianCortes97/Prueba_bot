import MetaTrader5 as mt5
import pandas as pd
import requests
from datetime import datetime, timedelta
import time
import os

# ===========================
# Configuración del bot
# ===========================
SYMBOL = "Step Index"
CAPITAL_INICIAL = 1000
RIESGO_USD = 50
LIMITE_DIARIO = 0.05
INTERVALO_MINUTOS = 15
MAGIC_NUMBER = 123456
TRAILING_DISTANCE = 0.8
TRIGGER_RR = 2

# ===========================
# Configuración de Telegram
# ===========================
TELEGRAM_TOKEN = "7783097990:AAG0YdqLwKgEmU9fmHAlt_U9Uj3eEzY6p0g"
TELEGRAM_CHAT_ID = "960425952"

# ===========================
# Archivos
# ===========================
REGISTRO_PATH = "registros/operaciones.csv"
os.makedirs("registros", exist_ok=True)

# ===========================
# Funciones auxiliares
# ===========================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[Telegram] Error: {e}")

def registrar_operacion(data):
    df = pd.DataFrame([data])
    if not os.path.exists(REGISTRO_PATH):
        df.to_csv(REGISTRO_PATH, index=False)
    else:
        df.to_csv(REGISTRO_PATH, mode='a', header=False, index=False)

def calcular_volumen(sl_pips):
    volumen = RIESGO_USD / sl_pips
    return round(max(min(volumen, 50.0), 0.1), 2)

def detectar_entrada():
    h1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 6)
    m15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 6)
    m5 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 6)
    if h1 is None or m15 is None or m5 is None:
        return None

    df_h1 = pd.DataFrame(h1)
    df_m15 = pd.DataFrame(m15)
    df_m5 = pd.DataFrame(m5)

    if df_h1["close"].iloc[-1] > df_h1["close"].iloc[0]:
        tendencia = "buy"
    elif df_h1["close"].iloc[-1] < df_h1["close"].iloc[0]:
        tendencia = "sell"
    else:
        return None

    if tendencia == "buy" and df_m15["close"].iloc[-1] <= df_m15["high"].iloc[:-1].max():
        return None
    if tendencia == "sell" and df_m15["close"].iloc[-1] >= df_m15["low"].iloc[:-1].min():
        return None

    vela = df_m5.iloc[-1]
    cuerpo = abs(vela["close"] - vela["open"])
    mecha = vela["high"] - vela["low"]
    rango = mecha

    if cuerpo <= mecha * 0.5 or rango < 6:
        return None

    hora_actual = datetime.now().hour
    if hora_actual == 7:
        return None

    return tendencia

def enviar_orden(tendencia):
    if not mt5.symbol_select(SYMBOL, True):
        enviar_telegram(f"❌ No se pudo seleccionar el símbolo: {SYMBOL}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    tick = mt5.symbol_info_tick(SYMBOL)
    if not symbol_info or not tick:
        enviar_telegram(f"❌ Error al obtener datos de {SYMBOL}")
        return

    point = symbol_info.point
    min_stop = symbol_info.trade_stops_level * point
    price = tick.ask if tendencia == "buy" else tick.bid

    zona_sl = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 1, 6)
    df_zona = pd.DataFrame(zona_sl)
    if tendencia == "sell":
        raw_sl = df_zona["high"].max() + 0.3
        sl = raw_sl if abs(price - raw_sl) >= min_stop and abs(price - raw_sl) > 6 else price + max(min_stop, 6.1)
    else:
        raw_sl = df_zona["low"].min() - 0.3
        sl = raw_sl if abs(price - raw_sl) >= min_stop and abs(price - raw_sl) > 6 else price - max(min_stop, 6.1)

    sl = round(sl, 1)
    sl_pips = abs(price - sl)
    volume = calcular_volumen(sl_pips)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if tendencia == "buy" else mt5.ORDER_TYPE_SELL,
        "price": round(price, 1),
        "sl": sl,
        "tp": 0,
        "deviation": 10,
        "magic": MAGIC_NUMBER,
        "comment": "Trailing Entry",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result is None:
        error = mt5.last_error()
        enviar_telegram(f"❌ Error: order_send devolvió None\nMT5 Error: {error}")
        return

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        enviar_telegram(f"✅ ORDEN EJECUTADA: {SYMBOL} {tendencia.upper()}\nPrecio: {round(price, 1)} | SL: {sl} | Volumen: {volume}")
        registrar_operacion({
            "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": SYMBOL,
            "tipo": tendencia,
            "precio": round(price, 1),
            "sl": sl,
            "tp": 0,
            "volumen": volume,
            "resultado": result.retcode
        })
    else:
        enviar_telegram(f"❌ ERROR {result.retcode} al enviar orden {SYMBOL} {tendencia.upper()}")

def gestionar_trailing():
    posiciones = mt5.positions_get(symbol=SYMBOL)
    if not posiciones:
        return

    for pos in posiciones:
        sl = pos.sl
        precio = pos.price_open
        vol = pos.volume
        tipo = "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell"
        actual = mt5.symbol_info_tick(SYMBOL)
        precio_actual = actual.bid if tipo == "sell" else actual.ask
        ganancia_flotante = (precio_actual - precio) if tipo == "buy" else (precio - precio_actual)

        if ganancia_flotante >= TRAILING_DISTANCE * TRIGGER_RR:
            if tipo == "buy":
                nuevo_sl = max(sl, precio_actual - TRAILING_DISTANCE)
            else:
                nuevo_sl = min(sl, precio_actual + TRAILING_DISTANCE)

            nuevo_sl = round(nuevo_sl, 1)

            if nuevo_sl != sl:
                modificar = mt5.order_send({
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "sl": nuevo_sl,
                    "tp": 0,
                    "symbol": SYMBOL,
                    "magic": MAGIC_NUMBER
                })
                if modificar and modificar.retcode == mt5.TRADE_RETCODE_DONE:
                    enviar_telegram(f"🔁 SL actualizado para {SYMBOL} ({tipo.upper()}): Nuevo SL = {nuevo_sl}")

def enviar_resumen():
    if not os.path.exists(REGISTRO_PATH):
        return
    df = pd.read_csv(REGISTRO_PATH)
    hoy = datetime.now().strftime("%Y-%m-%d")
    df_hoy = df[df['fecha'].str.startswith(hoy)]
    if df_hoy.empty:
        return
    ganadas = df_hoy[df_hoy['resultado'] == 10009].shape[0]
    perdidas = df_hoy[df_hoy['resultado'] != 10009].shape[0]
    total = ganadas + perdidas
    mensaje = f"📊 RESUMEN DIARIO {hoy}\nOperaciones: {total}\n✅ Ganadas: {ganadas} | ❌ Perdidas: {perdidas}"
    enviar_telegram(mensaje)

# ===========================
# Inicialización principal
# ===========================
if not mt5.initialize():
    enviar_telegram("❌ No se pudo conectar a MT5")
    quit()

enviar_telegram("🤖 Bot activo para Step Index (estrategia optimizada + trailing real)")
resumen_enviado = False
ultimo_dia = datetime.now().day

while True:
    ahora = datetime.now()
    if ahora.day != ultimo_dia:
        resumen_enviado = False
        ultimo_dia = ahora.day
        enviar_telegram("🔁 Nuevo día operativo iniciado")

    if ahora.hour == 23 and ahora.minute >= 55 and not resumen_enviado:
        enviar_resumen()
        resumen_enviado = True

    direccion = detectar_entrada()
    if direccion:
        enviar_orden(direccion)

    gestionar_trailing()
    time.sleep(INTERVALO_MINUTOS * 60)
