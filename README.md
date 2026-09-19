# Observatorio Normativo Universitario

Proyecto inicial para crear una web pública de seguimiento y consulta de normativa universitaria relevante para la Universidad de Murcia.

## Estado actual

Esta primera versión es únicamente una **maqueta funcional**. Los registros incluidos son ejemplos de prueba y no deben utilizarse todavía como fuente jurídica.

## Objetivo

El proyecto terminará integrando:

- Normativa estatal.
- Normativa de la Región de Murcia.
- Normativa interna de la Universidad de Murcia.
- Clasificación por materias, colectivos, rango y estado.
- Buscador y filtros.
- Historial de modificaciones.
- Detección automática de cambios.
- Enlaces permanentes a fuentes oficiales.
- Registro de fecha de comprobación y estado de verificación.

## Estructura

- `src/`: interfaz web.
- `data/`: datos normativos.
- `scripts/`: futuras automatizaciones de BOE, BORM y UMU.
- `.github/workflows/`: futuras tareas automáticas.
- `public/`: recursos públicos.

## Desarrollo local

No es necesario para el primer paso. Más adelante, si se quiere ejecutar en un ordenador:

```bash
npm install
npm run dev
```

## Construcción

```bash
npm run build
```

La web generada queda en la carpeta `dist/`.
