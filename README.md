# La Fragua — fábrica de código agéntica (VForgeLab)

El taller de ejecución de VForgeLab: una cola de despacho Postgres que
alimenta un daemon con workers que escriben, corren y prueban código con
agentes, y un supervisor independiente que valida cada entrega con evidencia
literal antes de aprobarla. Los ciclos exitosos se vectorizan como patrones
en el Brain — la fábrica aprende de cada trabajo.

## Componentes
- `vulcano_daemon.py` — daemon de producción (74 KB, un archivo): workers
  concurrentes con reclamo atómico (FOR UPDATE SKIP LOCKED), executores
  intercambiables (shell, agente de código, browser), watchdog de timeout,
  reporte obligatorio al Brain antes de marcar done, y loop de aprendizaje
  que vectoriza cada trabajo APROBADO.
- `review.mjs` / `review.js` — supervisor independiente: revisa el diff con
  un modelo externo y exige cita literal verificable por cada hallazgo
  (UNSUPPORTED_REVIEW_FINDING si la evidencia no existe en el código).

## Configuración
```
pip install psycopg2-binary
export NEON_DATABASE_URL_API=postgres://...   # la cola de despacho
python3 vulcano_daemon.py                      # los workers arrancan
```

## Motor NVIDIA Nemotron en Nebius Token Factory
El executor de agente es un cliente OpenAI-compatible; apúntalo a Nebius
Token Factory para que los agentes construyan con Nemotron:
```
export SALA_BASE_URL=https://api.tokenfactory.nebius.com/v1
export SALA_TXT_MODEL=nvidia/nemotron-3-super
export NEBIUS_API_KEY=...
```
Nemotron 3 Ultra para planificar builds complejos; Nano/Super para el ciclo
rápido escribir-probar-corregir, sin quemar créditos.

## Operación real (30 oct 2026)
Daemon en producción continua en un Hetzner propio (16 cores, 30 GB RAM);
trabajos done verificados en la cola y 4 sellos APROBADOS del supervisor con
evidencia verificada en una sola tarde. El sistema reporta su estado real —
nunca hardware inventado.
