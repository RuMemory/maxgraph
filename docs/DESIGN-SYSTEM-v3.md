# 🎨 Design System v3 — «Beehiiv Dark» (адаптация)

**Дата:** 2026-10-06 · **Заменяет:** DESIGN-SYSTEM-v2 (Neon Cosmos HUD) — v2 остаётся в истории, страницы переводятся на v3 целиком.
**Источник токенов:** beehiiv.com (dark, Ecosystem Index) через Inspo — адаптируем, не копируем.

## Принцип

От неон-космоса к **сдержанному SaaS-dark**: плоские тёмные поверхности, 1px-границы вместо свечений, типографика вместо декора. Никаких звёзд, аврор, text-shadow и box-shadow-неона. Живость — через ритм сетки, вес шрифта и ОДИН фирменный мотив (сфера-граф на лендинге, граф на дашборде) в приглушённой палитре.

## Жёсткие правила (наследуются из v2, не ослаблять)

1. Один файл на страницу, **ноль внешних URL** (ни шрифтов, ни CDN, ни картинок).
2. Данные сервера — только через `textContent` (CSP: `default-src 'self'`, inline разрешён).
3. `prefers-reduced-motion`, `prefers-reduced-transparency`, `prefers-contrast: more` — обязательные режимы.
4. Русский язык UI; лендинг — i18n RU/EN (`data-i18n` + `mg_lang`).

## Токены (канонический блок :root для всех страниц)

```css
:root{
  /* поверхности */
  --bg-page:#060419; --surface-1:#0d0b28; --surface-2:#141230; --surface-3:#1c1940;
  /* текст */
  --text-1:#ffffff; --text-2:#f7f5ff; --text-3:#c4c2d6; --text-disabled:#4e4e6c;
  /* акценты */
  --blue:#2f39ba; --blue-hover:#2a33a3; --blue-text:#7079d5; --blue-50:#d4d7f5; --blue-400:#5b66cb;
  --pink:#ff5ec4; --pink-hover:#e040a8; --pink-pressed:#c42d8c; --pink-text:#ff5ec4; --on-pink:#060419;
  --lavender:#bcbef3;
  /* границы */
  --border-subtle:#ffffff0f; --border-default:#ffffff1f; --border-strong:#ffffff33;
  --border-blue:#2f39ba80; --border-pink:#ff5ec44d; --border-lavender:#9299e64d;
  /* радиусы */
  --r-s:4px; --r-m:8px; --r-xl:12px; --r-pill:24px;
  /* шаг сетки 4px: 8/16/20/24/40 */
  --sp-1:8px; --sp-2:16px; --sp-3:20px; --sp-4:24px; --sp-5:40px;
  /* статусы (единственные не-beehiiv цвета, семантика) */
  --ok:#8dc572; --err:#be6464; --warn:#e0b34e;
  /* шрифты: ClashGrotesk/Satoshi коммерческие и внешние — правило «0 внешних URL»
     запрещает подгрузку. Подстановка системных с сохранением ПРОПОРЦИЙ шкалы. */
  --font-disp:"Segoe UI Variable Display","Segoe UI",Inter,system-ui,sans-serif; /* ~ClashGrotesk: 700/800, tight */
  --font-body:"Segoe UI",Inter,system-ui,sans-serif;                              /* ~Satoshi */
  --font-mono:"Cascadia Code","JetBrains Mono",Consolas,monospace;
}
```

## Типографика (пропорции beehiiv, база 16px)

| Роль | Размер | Вес | Line-height | Трекинг |
|---|---|---|---|---|
| h1 | clamp(36px,5vw,60px) | 700 | 1.0 | -0.02em |
| h2 | clamp(28px,3.6vw,48px) | 700 | 1.0 | -0.02em |
| h3 | 18px | 700 | 1.56 | 0 |
| body | 16px | 400 | 1.5 | 0 |
| caption/label | 14px/12px | 500 | 1.43 | 0.06em uppercase для eyebrow |

## Компоненты

- **Кнопки:** primary = `background:var(--blue)` → hover `var(--blue-hover)`; accent (главный CTA) = `background:var(--pink); color:var(--on-pink)` → hover `--pink-hover`, active `--pink-pressed`; ghost = transparent + `border:1px solid var(--border-default)`, текст `--blue-50`, hover `border-color:var(--border-strong)`. Радиус `--r-m` (8px), без свечений, переход 150ms ease.
- **Панели/карточки:** `background:var(--surface-1); border:1px solid var(--border-default); border-radius:var(--r-xl)`. Поднятая — `var(--surface-2)`. Hover-акцент: `border-color:var(--border-blue)`. Никаких backdrop-blur-неонов и glow-теней.
- **Инпуты:** `background:var(--bg-page); border:1px solid var(--border-default); radius:--r-m`; focus: `border-color:var(--blue-text)`, без glow.
- **Eyebrow/бейджи:** mono, uppercase, letter-spacing .12em, цвет `--lavender` или `--pink-text`, рамка `--border-pink` для «hot».
- **Код/конфиги:** `background:var(--bg-page); border:1px solid var(--border-subtle); radius:--r-m`; mono-шрифт.
- **Контейнер:** max-width 1440px (лендинг), 1200px (дашборд); сетка 8pt.

## Фирменный мотив (единственный «декор»)

- **Сфера-граф (лендинг)** и **живой граф (дашборд)** остаются, но перекрашиваются: узлы `#7079d5`/`#bcbef3`/`#ff5ec4` (2 акцентных), дуги `#2f39ba`, БЕЗ glow/теней, `globalCompositeOperation` обычный. Фон секции — `--bg-page`.
- Исключение из «ноль внешних URL» по-прежнему недопустимо: og.png лежит на том же origin (`/og.png`).

## Что удаляем из v2 (чек-лист для миграции страницы)

- `.stars`/`.aurora` слои и их keyframes; `text-shadow`; неоновые `box-shadow`/glow-токены (`--glow-*`); clip-path «уголки» HUD-панелей; анимированные градиентные рамки.
- Старые цвета `#04070F/#46F0FF/#FF5BD8/#8A6BFF/#38D39F/#FFC94D/#FF6B8A` → заменить на токены выше (status-цвета ok/err/warn — только для статусов).
- `backdrop-filter` и полупрозрачные `rgba()`-панели → плоские `--surface-*`.

## Миграция страниц (v0.12.0)

| Страница | Файл | Примечания |
|---|---|---|
| Лендинг | `app/landing.html` | Сфера перекрашена; демо-дашборд → плоские карточки; i18n RU/EN сохранить полностью |
| Дашборд | `app/dashboard.html` | Граф — фирменный мотив; легенда/панели плоские |
| Статус оплаты | `app/billing.html` | Минимальная страница: карточка по центру |
| Success | в `app/app.py` `_success_page` | Тот же токен-блок в её `<style>` |
