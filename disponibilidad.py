#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sistema de disponibilidad de horas — Ps. Enrique Campillay
===========================================================
Lee la ocupación del calendario de Google (solo libre/ocupado, sin
títulos ni datos de pacientes), la cruza con la grilla semanal de
atención y genera:

  1. docs/index.html  -> página pública con las horas libres
  2. Reporte de texto -> enviado por WhatsApp (CallMeBot) y correo

Uso:
  python disponibilidad.py                  # actualiza la página
  python disponibilidad.py --reporte        # además envía el reporte
  python disponibilidad.py --solo-a-las 8   # envía solo si son las 8 hora local
  python disponibilidad.py --mock mock_ocupados.json   # prueba sin Google

Privacidad: el acceso al calendario es "libre/ocupado" únicamente.
Este programa nunca ve, guarda ni transmite nombres de pacientes.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import yaml

DIAS = {0: "lun", 1: "mar", 2: "mie", 3: "jue", 4: "vie", 5: "sab", 6: "dom"}
DIAS_LARGO = {0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves",
              4: "Viernes", 5: "Sábado", 6: "Domingo"}
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

AQUI = os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------------
# Configuración y rango de fechas
# ------------------------------------------------------------------

def cargar_config():
    with open(os.path.join(AQUI, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def rango_fechas(hoy):
    """Desde mañana hasta el domingo de la próxima semana."""
    inicio = hoy + timedelta(days=1)
    dias_hasta_domingo = 6 - hoy.weekday()
    fin = hoy + timedelta(days=dias_hasta_domingo + 7)
    return inicio, fin


# ------------------------------------------------------------------
# Lectura de ocupación (Google Calendar, solo libre/ocupado)
# ------------------------------------------------------------------

def cliente_google():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    cred_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not cred_json:
        sys.exit("Falta la variable de entorno GOOGLE_CREDENTIALS")
    info = json.loads(cred_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def leer_ocupados(servicio, cfg, inicio_dt, fin_dt):
    """Bloques ocupados vía freebusy (no expone títulos ni detalles)."""
    cuerpo = {
        "timeMin": inicio_dt.isoformat(),
        "timeMax": fin_dt.isoformat(),
        "timeZone": cfg["zona_horaria"],
        "items": [{"id": cfg["calendario_principal"]}],
    }
    r = servicio.freebusy().query(body=cuerpo).execute()
    tz = ZoneInfo(cfg["zona_horaria"])
    ocupados = []
    for cal in r.get("calendars", {}).values():
        for b in cal.get("busy", []):
            ocupados.append((
                datetime.fromisoformat(b["start"]).astimezone(tz),
                datetime.fromisoformat(b["end"]).astimezone(tz),
            ))
    return ocupados


def leer_feriados(servicio, cfg, inicio_dt, fin_dt):
    """Fechas de feriados de Chile dentro del rango."""
    try:
        r = servicio.events().list(
            calendarId=cfg["calendario_feriados"],
            timeMin=inicio_dt.isoformat(), timeMax=fin_dt.isoformat(),
            singleEvents=True).execute()
    except Exception:
        return set()
    feriados = set()
    for ev in r.get("items", []):
        d = ev.get("start", {}).get("date")
        if d:
            feriados.add(date.fromisoformat(d))
    return feriados


def leer_aperturas(servicio, cfg, inicio_dt, fin_dt):
    """Eventos del calendario 'Aperturas' (sábados u horas extra).
    Este calendario solo contiene disponibilidad, nunca datos clínicos."""
    cal_id = (cfg.get("calendario_aperturas") or "").strip()
    if not cal_id:
        return []
    tz = ZoneInfo(cfg["zona_horaria"])
    try:
        r = servicio.events().list(
            calendarId=cal_id, timeMin=inicio_dt.isoformat(),
            timeMax=fin_dt.isoformat(), singleEvents=True).execute()
    except Exception:
        return []
    aperturas = []
    for ev in r.get("items", []):
        s = ev.get("start", {}).get("dateTime")
        e = ev.get("end", {}).get("dateTime")
        if s and e:
            aperturas.append((
                datetime.fromisoformat(s).astimezone(tz),
                datetime.fromisoformat(e).astimezone(tz),
            ))
    return aperturas


# ------------------------------------------------------------------
# Cálculo de disponibilidad
# ------------------------------------------------------------------

def se_solapan(a_ini, a_fin, b_ini, b_fin):
    return a_ini < b_fin and b_ini < a_fin


def calcular_dias(cfg, ocupados, feriados, aperturas, inicio, fin):
    """Devuelve lista de días, cada uno con sus bloques y estado."""
    tz = ZoneInfo(cfg["zona_horaria"])
    dur = timedelta(minutes=cfg["duracion_sesion_min"])
    grilla = cfg["grilla"]
    dias = []
    d = inicio
    while d <= fin:
        clave = DIAS[d.weekday()]
        periodos = []
        cfg_dia = grilla.get(clave)
        if cfg_dia and d not in feriados:
            agrupado = {}  # nombre_periodo -> lista de bloques
            for nombre, p in cfg_dia.items():
                base = "manana" if nombre.startswith("manana") else "tarde"
                for h in p["horas"]:
                    hh, mm = map(int, h.split(":"))
                    ini = datetime.combine(d, time(hh, mm), tzinfo=tz)
                    libre = not any(se_solapan(ini, ini + dur, o1, o2)
                                    for o1, o2 in ocupados)
                    agrupado.setdefault(base, []).append({
                        "hora": h, "modo": p["modo"], "libre": libre})
            for base in ("manana", "tarde"):
                if base in agrupado:
                    bloques = sorted(agrupado[base], key=lambda b: b["hora"])
                    modos = {b["modo"] for b in bloques}
                    modo = "mixto" if "mixto" in modos else "linea"
                    periodos.append({"nombre": base, "modo": modo,
                                     "bloques": bloques})
        # Aperturas extraordinarias (p. ej. sábados): bloques de 60 min
        # cada 70 min dentro del rango del evento, solo en línea.
        extra = []
        for a_ini, a_fin in aperturas:
            if a_ini.date() != d:
                continue
            t = a_ini
            while t + dur <= a_fin:
                libre = not any(se_solapan(t, t + dur, o1, o2)
                                for o1, o2 in ocupados)
                extra.append({"hora": t.strftime("%H:%M"),
                              "modo": "linea", "libre": libre})
                t += dur + timedelta(minutes=10)
        if extra:
            nombre = "manana" if extra[0]["hora"] < "14:00" else "tarde"
            periodos.append({"nombre": nombre, "modo": "linea",
                             "bloques": extra, "extraordinario": True})
        if periodos:
            dias.append({"fecha": d, "feriado": d in feriados,
                         "periodos": periodos})
        elif cfg_dia and d in feriados:
            dias.append({"fecha": d, "feriado": True, "periodos": []})
        d += timedelta(days=1)
    return dias


def detectar_anomalias(cfg, ocupados):
    """Bloques ocupados a horas inusuales (posible error de digitación)."""
    avisos = []
    for ini, fin_ in ocupados:
        if ini.hour < 7 or ini.hour >= 21:
            avisos.append("Evento a hora inusual: %s de %s a %s" % (
                ini.strftime("%a %d/%m"), ini.strftime("%H:%M"),
                fin_.strftime("%H:%M")))
    return avisos


# ------------------------------------------------------------------
# Salidas: página HTML y reporte de texto
# ------------------------------------------------------------------

def etiqueta_fecha(f):
    return "%s %d de %s" % (DIAS_LARGO[f.weekday()], f.day,
                            MESES[f.month - 1].capitalize())


def generar_html(cfg, dias, ahora):
    num = cfg["whatsapp_numero"]
    filas = []
    for dia in dias:
        f = dia["fecha"]
        filas.append('<section class="dia"><h2>📌 %s</h2>' % etiqueta_fecha(f))
        if dia["feriado"]:
            filas.append('<p class="vacio">Feriado — sin atención</p>')
        for p in dia["periodos"]:
            nombre = "Mañana" if p["nombre"] == "manana" else "Tarde"
            icono = "🏥" if p["modo"] == "mixto" else "🌐"
            desc = ("presencial o en línea" if p["modo"] == "mixto"
                    else "solo en línea")
            extra = " · apertura especial" if p.get("extraordinario") else ""
            filas.append('<h3>%s %s <small>(%s%s)</small></h3>'
                         % (icono, nombre, desc, extra))
            libres = [b for b in p["bloques"] if b["libre"]]
            if not libres:
                filas.append('<p class="vacio">❌ Sin disponibilidad</p>')
            else:
                filas.append('<div class="horas">')
                for b in libres:
                    icono_b = "🏥" if b["modo"] == "mixto" else "🌐"
                    msg = urllib.parse.quote(
                        "Hola, quiero reservar una hora el %s a las %s."
                        % (etiqueta_fecha(f), b["hora"]))
                    filas.append(
                        '<a class="hora" href="https://wa.me/%s?text=%s">'
                        '%s %s</a>' % (num, msg, icono_b, b["hora"]))
                filas.append('</div>')
        filas.append('</section>')

    firma = "<br>".join(cfg["firma"])
    html = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>%(titulo)s</title>
<style>
  :root { --verde: #1f7a5c; --claro: #eef7f3; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 0; background: #f6f6f4; color: #222; }
  .marco { max-width: 480px; margin: 0 auto; padding: 16px; }
  header { background: var(--verde); color: #fff; border-radius: 12px;
           padding: 18px; margin-bottom: 14px; }
  header h1 { margin: 0 0 6px; font-size: 1.25rem; }
  header p { margin: 2px 0; font-size: .85rem; opacity: .92; }
  .dia { background: #fff; border-radius: 12px; padding: 14px 16px;
         margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .dia h2 { margin: 0 0 4px; font-size: 1.05rem; color: var(--verde); }
  .dia h3 { margin: 10px 0 6px; font-size: .9rem; }
  .dia h3 small { font-weight: normal; color: #666; }
  .horas { display: flex; flex-wrap: wrap; gap: 8px; }
  .hora { display: inline-block; padding: 8px 14px; border-radius: 20px;
          background: var(--claro); color: var(--verde); font-weight: 600;
          text-decoration: none; border: 1px solid var(--verde);
          font-size: .95rem; }
  .hora:active { background: var(--verde); color: #fff; }
  .vacio { color: #999; font-size: .85rem; margin: 4px 0; }
  .pago { background: #fff8e6; border-radius: 12px; padding: 12px 16px;
          font-size: .85rem; margin-bottom: 12px; }
  footer { font-size: .78rem; color: #555; text-align: center;
           padding: 8px 4px 24px; line-height: 1.5; }
  .nota { font-size: .8rem; color: #666; text-align: center;
          margin-bottom: 14px; }
</style>
</head>
<body>
<div class="marco">
<header>
  <h1>📅 Horas disponibles</h1>
  <p>%(centro)s</p>
  <p>Actualizado: %(actualizado)s</p>
</header>
<p class="nota">Toca una hora para reservarla por WhatsApp.
La hora queda confirmada solo cuando recibas mi respuesta.</p>
%(cuerpo)s
<div class="pago">💳 <strong>Recordatorio de pago:</strong> %(pago)s</div>
<footer>%(firma)s</footer>
</div>
</body>
</html>""" % {
        "titulo": cfg["titulo_pagina"],
        "centro": cfg["centro"],
        "actualizado": ahora.strftime("%d/%m/%Y %H:%M"),
        "cuerpo": "\n".join(filas),
        "pago": cfg["recordatorio_pago"].strip(),
        "firma": firma,
    }
    return html


def generar_reporte(cfg, dias, ahora, avisos):
    """Reporte privado para Enrique (formato de su plantilla)."""
    lineas = ["📅 DISPONIBILIDAD — %s" % ahora.strftime("%d/%m/%Y %H:%M"),
              "━━━━━━━━━━━━"]
    total = 0
    for dia in dias:
        f = dia["fecha"]
        lineas.append("📌 %s" % etiqueta_fecha(f))
        if dia["feriado"]:
            lineas.append("   Feriado — sin atención")
            continue
        for p in dia["periodos"]:
            nombre = "Mañana" if p["nombre"] == "manana" else "Tarde"
            icono = "🏥" if p["modo"] == "mixto" else "🌐"
            libres = [b for b in p["bloques"] if b["libre"]]
            total += len(libres)
            if libres:
                partes = []
                for b in libres:
                    if b["modo"] != p["modo"]:
                        ic = "🏥" if b["modo"] == "mixto" else "🌐"
                        partes.append("%s (%s)" % (b["hora"], ic))
                    else:
                        partes.append(b["hora"])
                lineas.append("   %s %s: %s" % (icono, nombre,
                                                " · ".join(partes)))
            else:
                lineas.append("   %s %s: ❌ sin disponibilidad" % (icono,
                                                                   nombre))
    lineas.append("━━━━━━━━━━━━")
    lineas.append("Total de horas libres: %d" % total)
    if avisos:
        lineas.append("")
        lineas.append("⚠️ REVISAR EN EL CALENDARIO:")
        for a in avisos:
            lineas.append("• " + a)
    return "\n".join(lineas)


# ------------------------------------------------------------------
# Envío: CallMeBot (WhatsApp propio) y correo de respaldo
# ------------------------------------------------------------------

def enviar_callmebot(texto):
    tel = os.environ.get("CALLMEBOT_PHONE")
    clave = os.environ.get("CALLMEBOT_APIKEY")
    if not (tel and clave):
        print("CallMeBot no configurado; se omite WhatsApp.")
        return False
    url = ("https://api.callmebot.com/whatsapp.php?phone=%s&apikey=%s&text=%s"
           % (tel, clave, urllib.parse.quote(texto)))
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            print("CallMeBot:", r.status)
            return r.status == 200
    except Exception as e:
        print("Error CallMeBot:", e)
        return False


def enviar_correo(asunto, texto):
    import smtplib
    from email.mime.text import MIMEText
    usuario = os.environ.get("GMAIL_USER")
    clave = os.environ.get("GMAIL_APP_PASSWORD")
    if not (usuario and clave):
        print("Correo no configurado; se omite.")
        return False
    msg = MIMEText(texto, "plain", "utf-8")
    msg["Subject"] = asunto
    msg["From"] = usuario
    msg["To"] = usuario
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(usuario, clave)
            s.send_message(msg)
        print("Correo enviado.")
        return True
    except Exception as e:
        print("Error correo:", e)
        return False


# ------------------------------------------------------------------
# Programa principal
# ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reporte", action="store_true",
                    help="Enviar reporte por WhatsApp y correo")
    ap.add_argument("--solo-a-las", type=int, default=None,
                    help="Enviar reporte solo si es esta hora local")
    ap.add_argument("--mock", default=None,
                    help="Archivo JSON con ocupación simulada (pruebas)")
    args = ap.parse_args()

    cfg = cargar_config()
    tz = ZoneInfo(cfg["zona_horaria"])
    ahora = datetime.now(tz)
    inicio, fin = rango_fechas(ahora.date())
    inicio_dt = datetime.combine(inicio, time(0, 0), tzinfo=tz)
    fin_dt = datetime.combine(fin, time(23, 59), tzinfo=tz)

    if args.mock:
        with open(args.mock, encoding="utf-8") as f:
            datos = json.load(f)
        ocupados = [(datetime.fromisoformat(a).astimezone(tz),
                     datetime.fromisoformat(b).astimezone(tz))
                    for a, b in datos.get("ocupados", [])]
        feriados = {date.fromisoformat(x) for x in datos.get("feriados", [])}
        aperturas = [(datetime.fromisoformat(a).astimezone(tz),
                      datetime.fromisoformat(b).astimezone(tz))
                     for a, b in datos.get("aperturas", [])]
    else:
        servicio = cliente_google()
        ocupados = leer_ocupados(servicio, cfg, inicio_dt, fin_dt)
        feriados = leer_feriados(servicio, cfg, inicio_dt, fin_dt)
        aperturas = leer_aperturas(servicio, cfg, inicio_dt, fin_dt)

    dias = calcular_dias(cfg, ocupados, feriados, aperturas, inicio, fin)
    avisos = detectar_anomalias(cfg, ocupados)

    html = generar_html(cfg, dias, ahora)
    salida = os.path.join(AQUI, "docs", "index.html")
    os.makedirs(os.path.dirname(salida), exist_ok=True)
    with open(salida, "w", encoding="utf-8") as f:
        f.write(html)
    print("Página generada:", salida)

    if args.reporte:
        if args.solo_a_las is not None and ahora.hour != args.solo_a_las:
            print("No son las %d:00 locales (%s); no se envía reporte."
                  % (args.solo_a_las, ahora.strftime("%H:%M")))
            return
        texto = generar_reporte(cfg, dias, ahora, avisos)
        print("\n" + texto + "\n")
        ok_wsp = enviar_callmebot(texto)
        ok_mail = enviar_correo("Disponibilidad de horas — reporte diario",
                                texto)
        if not (ok_wsp or ok_mail):
            sys.exit("No se pudo enviar el reporte por ningún canal.")


if __name__ == "__main__":
    main()
