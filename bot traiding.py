import ccxt
import pandas as pd
import requests
from datetime import datetime, timedelta
import time
import os
import numpy as np

# ===========================
# Configuración del bot
# ===========================
SYMBOL = "BTC/USDT"  # Reemplazamos Step Index por un par compatible con el exchange
EXCHANGE = "binance"  # Puedes cambiarlo por el exchange que prefieras
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

# Inicialización del exchange (en modo de prueba/sandbox)
exchange = getattr(ccxt, EXCHANGE)({
    'apiKey': 'TU_API_KEY',  # Reemplaza con tu API KEY
    'secret': 'TU_API_SECRET',  # Reemplaza con tu API SECRET
    'enableRateLimit': True,
})

# Usa el modo sandbox/testnet si está disponible
if hasattr(exchange, 'setSandboxMode'):
    exchange.setSandboxMode(True)

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

def obtener_datos_ohlcv(symbol, timeframe, limit=6):
    """Obtiene datos OHLCV del exchange"""
    try:
        # Convertimos el timeframe de MT5 a formato CCXT
        tf_map = {
            "M5": "5m",
            "M15": "15m",
            "H1": "1h"
        }
        ccxt_tf = tf_map.get(timeframe, "1h")
        
        # Obtenemos los datos
        ohlcv = exchange.fetch_ohlcv(symbol, ccxt_tf, limit=limit)
        
        # Convertimos a DataFrame similar al formato MT5
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        return df
    except Exception as e:
        print(f"Error al obtener datos OHLCV: {e}")
        return None

def detectar_entrada():
    """Detecta oportunidades de entrada basadas en la estrategia original"""
    try:
        # Obtener datos para diferentes timeframes
        df_h1 = obtener_datos_ohlcv(SYMBOL, "H1", 6)
        df_m15 = obtener_datos_ohlcv(SYMBOL, "M15", 6)
        df_m5 = obtener_datos_ohlcv(SYMBOL, "M5", 6)
        
        if df_h1 is None or df_m15 is None or df_m5 is None:
            print("No se pudieron obtener todos los datos necesarios")
            return None
            
        # Determinar tendencia
        if df_h1["close"].iloc[-1] > df_h1["close"].iloc[0]:
            tendencia = "buy"
        elif df_h1["close"].iloc[-1] < df_h1["close"].iloc[0]:
            tendencia = "sell"
        else:
            return None
            
        # Validar condiciones de entrada
        if tendencia == "buy" and df_m15["close"].iloc[-1] <= df_m15["high"].iloc[:-1].max():
            return None
        if tendencia == "sell" and df_m15["close"].iloc[-1] >= df_m15["low"].iloc[:-1].min():
            return None
            
        # Analizar vela reciente
        vela = df_m5.iloc[-1]
        cuerpo = abs(vela["close"] - vela["open"])
        mecha = vela["high"] - vela["low"]
        rango = mecha
        
        # Validar patrones de vela
        precio_promedio = (vela["high"] + vela["low"]) / 2
        rango_relativo = mecha / precio_promedio * 100  # Convertimos a porcentaje
        
        if cuerpo <= mecha * 0.5 or rango_relativo < 0.6:  # Adaptamos el valor 6 a un porcentaje relativo
            return None
            
        # Restricción horaria
        hora_actual = datetime.now().hour
        if hora_actual == 7:
            return None
            
        return tendencia
    except Exception as e:
        print(f"Error en detectar_entrada: {e}")
        return None

def enviar_orden(tendencia):
    """Envía una orden al exchange basada en la dirección detectada"""
    try:
        # Obtener datos de mercado
        ticker = exchange.fetch_ticker(SYMBOL)
        if not ticker:
            enviar_telegram(f"❌ Error al obtener datos de {SYMBOL}")
            return
            
        # Obtener precio actual
        price = ticker['ask'] if tendencia == "buy" else ticker['bid']
        
        # Calcular stop loss
        zona_sl = obtener_datos_ohlcv(SYMBOL, "M5", 6)
        df_zona = zona_sl
        
        # Calcular nivel de stop loss
        precio_promedio = price
        ajuste = precio_promedio * 0.003  # 0.3% como equivalente relativo
        
        if tendencia == "sell":
            raw_sl = df_zona["high"].max() + ajuste
            min_stop = price * 0.006  # Equivalente a 6 pips en términos relativos
            sl = raw_sl if abs(price - raw_sl) >= min_stop else price + min_stop
        else:
            raw_sl = df_zona["low"].min() - ajuste
            min_stop = price * 0.006  # Equivalente a 6 pips en términos relativos
            sl = raw_sl if abs(price - raw_sl) >= min_stop else price - min_stop
            
        sl = round(sl, exchange.markets[SYMBOL]['precision']['price'])
        
        # Calcular tamaño de posición
        sl_pips = abs(price - sl)
        volume = calcular_volumen(sl_pips)
        
        # Ajustar volumen a la precisión del exchange
        amount_precision = exchange.markets[SYMBOL]['precision']['amount']
        volume = round(volume, amount_precision)
        
        # Enviar orden
        side = 'buy' if tendencia == "buy" else 'sell'
        
        # Primero creamos la orden de mercado
        orden = exchange.create_order(
            symbol=SYMBOL,
            type='market',
            side=side,
            amount=volume
        )
        
        if orden:
            # Creamos la orden de stop loss
            stop_order = exchange.create_order(
                symbol=SYMBOL,
                type='stop',
                side='sell' if tendencia == 'buy' else 'buy',
                amount=volume,
                price=sl,
                params={'stopPrice': sl}
            )
            
            enviar_telegram(f"✅ ORDEN EJECUTADA: {SYMBOL} {tendencia.upper()}\nPrecio: {price} | SL: {sl} | Volumen: {volume}")
            
            registrar_operacion({
                "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": SYMBOL,
                "tipo": tendencia,
                "precio": price,
                "sl": sl,
                "tp": 0,
                "volumen": volume,
                "resultado": orden['id']
            })
        else:
            enviar_telegram(f"❌ ERROR al enviar orden {SYMBOL} {tendencia.upper()}")
    except Exception as e:
        enviar_telegram(f"❌ Error al enviar orden: {e}")

def gestionar_trailing():
    """Gestiona el trailing stop de posiciones abiertas"""
    try:
        # Obtener posiciones abiertas
        posiciones = exchange.fetch_open_positions() if hasattr(exchange, 'fetch_open_positions') else []
        
        # Si el exchange no soporta fetch_open_positions, usamos una alternativa
        if not posiciones and hasattr(exchange, 'fetch_positions'):
            posiciones = exchange.fetch_positions()
        elif not posiciones:
            # Última alternativa: buscar órdenes abiertas
            ordenes = exchange.fetch_open_orders(SYMBOL)
            if not ordenes:
                return
                
        for pos in posiciones:
            # Extraer datos de la posición
            if not isinstance(pos, dict):
                continue
                
            # Verificar si la posición es para nuestro símbolo
            if 'symbol' in pos and pos['symbol'] != SYMBOL:
                continue
                
            # Obtener datos de la posición
            side = pos.get('side', '')
            entry_price = float(pos.get('entryPrice', 0))
            amount = float(pos.get('contracts', 0))
            
            if entry_price == 0 or amount == 0:
                continue
                
            # Obtener precio actual
            ticker = exchange.fetch_ticker(SYMBOL)
            precio_actual = ticker['bid'] if side == 'sell' else ticker['ask']
            
            # Calcular ganancia flotante
            tipo = "buy" if side == 'long' else "sell"
            ganancia_flotante = abs(precio_actual - entry_price)
            ganancia_relativa = ganancia_flotante / entry_price
            
            # Verificar si debemos ajustar el trailing stop
            if ganancia_relativa >= TRAILING_DISTANCE * TRIGGER_RR / 100:
                # Calcular nuevo stop loss
                if tipo == "buy":
                    nuevo_sl = precio_actual - (precio_actual * TRAILING_DISTANCE / 100)
                else:
                    nuevo_sl = precio_actual + (precio_actual * TRAILING_DISTANCE / 100)
                    
                nuevo_sl = round(nuevo_sl, exchange.markets[SYMBOL]['precision']['price'])
                
                # Actualizar stop loss
                try:
                    # Cancelar orden stop anterior
                    ordenes_stop = exchange.fetch_open_orders(SYMBOL)
                    for orden in ordenes_stop:
                        if orden['type'] == 'stop' or orden['type'] == 'stop_loss':
                            exchange.cancel_order(orden['id'], SYMBOL)
                    
                    # Crear nueva orden stop
                    exchange.create_order(
                        symbol=SYMBOL,
                        type='stop',
                        side='sell' if tipo == 'buy' else 'buy',
                        amount=amount,
                        price=nuevo_sl,
                        params={'stopPrice': nuevo_sl}
                    )
                    
                    enviar_telegram(f"🔁 SL actualizado para {SYMBOL} ({tipo.upper()}): Nuevo SL = {nuevo_sl}")
                except Exception as e:
                    print(f"Error al actualizar stop loss: {e}")
    except Exception as e:
        print(f"Error en gestionar_trailing: {e}")

def enviar_resumen():
    """Envía un resumen diario de operaciones"""
    if not os.path.exists(REGISTRO_PATH):
        return
        
    df = pd.read_csv(REGISTRO_PATH)
    hoy = datetime.now().strftime("%Y-%m-%d")
    df_hoy = df[df['fecha'].str.startswith(hoy)]
    
    if df_hoy.empty:
        return
        
    # Analizamos operaciones con resultados
    total = len(df_hoy)
    
    # Suponemos que las operaciones con resultado numérico son exitosas
    ganadas = df_hoy[df_hoy['resultado'].astype(str).str.isnumeric()].shape[0]
    perdidas = total - ganadas
    
    mensaje = f"📊 RESUMEN DIARIO {hoy}\nOperaciones: {total}\n✅ Ganadas: {ganadas} | ❌ Perdidas: {perdidas}"
    enviar_telegram(mensaje)

# ===========================
# Inicialización principal
# ===========================
try:
    # Verificar conexión con el exchange
    exchange.load_markets()
    enviar_telegram(f"🤖 Bot activo para {SYMBOL} en {exchange.id} (estrategia optimizada + trailing real)")
    
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
except Exception as e:
    enviar_telegram(f"❌ Error crítico en el bot: {e}")
    print(f"Error crítico: {e}")
