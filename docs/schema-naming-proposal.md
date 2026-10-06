# Новый вид двух баз: имена таблиц и столбцов

Предложение для Алексея и Андрея. Базы не сливаются: `lea_partners_db` (воронка 2) и `leadb` (воронка 1) остаются двумя схемами. Вид у них становится один и тот же. Открытие любой таблицы слева направо отвечает на вопросы: какой день, кто байер, откуда трафик, как называется креатив, какие цифры.

Физически старые имена лучше не переименовывать, пока загрузчик пишет в них. Сначала вьюха с новыми именами. Когда загрузчик и бот перейдут, вьюха становится таблицей.

Цифры и ограничения взяты из среза 2 октября 2026, файл `docs/database-architecture.md`.

## Как выглядит строка

Одинаковый порядок служебных столбцов в каждой факт-таблице:

```text
stat_date | funnel_code | buyer_id | buyer_name | source_code | placement_name | creative_name | метрики
```

`funnel_code` в первой базе всегда `funnel_2`, во второй всегда `funnel_1`. Выгрузка в таблицу тогда не теряет, из какой базы строка.

Смысл метрик один и тот же во всех таблицах:

| Столбец | Что это в речи |
|---|---|
| `starts` | старт инвайтера |
| `subscribers` | подписка на канал |
| `chats` | личка, лид |
| `registrations` | регистрация |
| `ftds` | первый депозит |
| `spend_usd` | расход в долларах; пусто, если выгрузки не было |

`card_chats` и `card_subscribers` — цифры с карточки креатива в Chatterfy. Это не `subscribers` из ленты каналов, и называются иначе, чтобы их не сложили.

## Общий словарь

Так проще читать и новую, и старую базу.

| Сейчас разбросано | Одно имя | Пример значения |
|---|---|---|
| `id`, `id_traf`, `traffer_id` | `buyer_id` | `1` |
| `traffer_name` | `buyer_name` | `NEW_Pavel` |
| `id_blog` | `placement_id` | `1` |
| `blogger` | `placement_name` | `РАМИ_ФБ` |
| `blogger_name` (леа / рами) | `brand_code` | `lea`, `rami` |
| `traf_type` (фб, ТГ, тт, Гугл) | `source_code` | `facebook`, `telegram`, `google`, `tiktok` |
| `creo_name` | `creative_name` | имя кампании или канала |
| `date` текстом `2026-10-02T00:00:00.000Z` | `stat_date` | дата `2026-10-02` |
| `budget`, `spend` | `spend_usd` | число или пусто |
| `count_start` | `starts` | |
| `count_sub`, `subs` на карточке | `subscribers` или `card_subscribers` | см. таблицу ниже |
| `count_chat`, `chats` на карточке | `chats` или `card_chats` | см. таблицу ниже |
| `count_reg` | `registrations` | |
| `count_ftd`, `num_of_first_deps` | `ftds` | |
| `comission` | `commission` | заодно правка опечатки |
| `country`, `country_name` | `country_code` + `country_name` | `SA` и `Saudi Arabia` |

`source_code` только из четырёх значений: `facebook`, `telegram`, `google`, `tiktok`. Старые `фб` / `ТГ` / `тт` / `Гугл` остаются в сырой колонке, во вьюхе уже нормальный код.

## Таблицы: как назвать

| Сейчас | Новое имя | Зачем открывают |
|---|---|---|
| `traffers` | `buyers` | кто байер |
| `bloggers` | `placements` | бренд и источник, не человек-блогер |
| `creos` | `creative_day` | расход и карточка креатива за день |
| `buyer_stats_today_start_sub` | `channel_day` | старты и подписки канала |
| `traffers_stat` | `buyer_day` | итог байера за день |
| `common_stats_report` | `project_day` | итог всей воронки за день |
| `users_stat` | `traders` | трейдер |
| `buyer_stats_today_start_sub_country` | `channel_country_day` | регистрации и FTD канала по стране |
| `countries` | `country_spend_day` | расход по стране, сейчас почти нулевая сетка |
| `countries_spend` | `country_spend_week` | расход страны за неделю |
| `countries_spend_month` | `country_spend_month` | расход страны за месяц |
| `countries_revenue` | `country_revenue_week` | комиссия и депозиты за неделю |
| `countries_revenue_month` | `country_revenue_month` | то же за месяц |
| `countries_cohorts_broker_pivot` | `cohort_week` | когорта страны, не байера |
| `countries_cohorts_broker_pivot_month` | `cohort_month` | то же по месяцу |
| `buyers_kpi` | `kpi_targets` | пороги, только старая база |
| `spend_fact` | `project_spend_day` | одна сумма расхода на день |
| `update_info` | `load_status` | когда загрузчик последний раз обновил байера |

Имена короче и про день, потому что строка почти везде уже «один день». Слово `today` в старых именах убирается: в таблице лежит история, не только сегодня.

Таблицы саппорта `operators`, `operators_ftd`, `operators_smeni` в этот нейминг не входят. К байеру они не привязаны.

Пустые и хвосты старой базы (`keitaro_pbs`, `buyer_stats_today`, `buyer_stats_today_start_sub_com`, `buyer_stats_today_start_sub_directbot`, `buyer_stats_country_today`, `countries_cohorts_broker`, `countries_cohorts_broker_uid`, `countries_cohorts_broker_uid_source`) физически не трогаем, пока не решим, какой контур живой. В новом виде их нет.

`buyer_day` сейчас есть только в новой базе. В старой заводим ту же таблицу, даже пустую, чтобы обе схемы выглядели одинаково.

## Столбцы по таблицам

Ниже только то, что переименовывается или добавляется. Порядок в новом виде — как в шапке раздела «Как выглядит строка», затем метрики.

### `buyers` (было `traffers`)

| Было | Стало |
|---|---|
| `id` | `buyer_id` |
| `traffer_name` | `buyer_name` |
| `traffer_status` | `status` |

Новые столбцы:

| Столбец | Пример | Откуда |
|---|---|---|
| `funnel_code` | `funnel_2` | константа схемы |
| `buyer_code` | `pavel` | заводим руками, латиница |
| `account_kind` | `person` или `cabinet` | руками; Farm = `cabinet` |
| `person_code` | `pavel` | у кабинета Farm сюда пишется человек; у обычного байера равен `buyer_code` |

Так в старой базе строка Farm сразу читается как кабинет Павла, а не как отдельный байер.

### `placements` (было `bloggers`)

| Было | Стало |
|---|---|
| `id` | `placement_id` |
| `blogger` | `placement_name` |
| `blogger_name` | остаётся сырьём, во вьюхе не показываем |
| `traf_type` | остаётся сырьём |
| `blogger_status` | `status` |
| `tier` | `tier` |
| `kpi` | `kpi_rate` |

Новые столбцы:

| Столбец | Пример | Откуда |
|---|---|---|
| `funnel_code` | `funnel_1` | константа схемы |
| `brand_code` | `rami` | из `blogger_name`: леа → `lea`, рами → `rami` |
| `source_code` | `facebook` | из `traf_type` |

`rekl_type` в новом виде не показываем: колонка пустая в обеих базах.

Пример одной строки новой базы:

```text
placement_id=1 | brand_code=rami | source_code=facebook | placement_name=РАМИ_ФБ | status=active
```

### `creative_day` (было `creos`)

Это главная таблица, которую сейчас трудно читать. Было: `date`, `id_traf`, `id_blog`, `id_creo`, `creo_name`, `scenario`, `budget`, `chats`, `subs`, `status`.

| Было | Стало | Почему |
|---|---|---|
| `date` | `stat_date` | дата, не текст с `T00:00:00.000Z` |
| `id_traf` | `buyer_id` | |
| `id_blog` | `placement_id` | |
| `creo_name` | `creative_name` | |
| `budget` | `spend_usd` | это расход, не бюджет кабинета |
| `chats` | `card_chats` | чаты карточки креатива |
| `subs` | `card_subscribers` | подписки карточки, не лента канала |
| `status` | `creative_status` | Работает / Стоп |

`id_creo` и `scenario` в новом виде не показываем: `id_creo` пустой или мусор, сценарий чаще пустой. `month` и `year` не показываем, день уже есть в `stat_date`.

Новые столбцы:

| Столбец | Пример | Откуда |
|---|---|---|
| `funnel_code` | `funnel_2` | константа |
| `buyer_name` | `NEW_Pavel` | из `buyers` |
| `brand_code` | `rami` | из `placements` |
| `source_code` | `facebook` | из `placements` |
| `placement_name` | `РАМИ_ФБ` | из `placements` |
| `creative_kind` | `ad_campaign` или `tg_channel` | правило по имени, пока нет настоящего id |
| `spend_status` | `present`, `missing`, `zero` | пустой budget → `missing`, `0` → `zero`, число > 0 → `present` |

Одна и та же строка Павла, как её видно сейчас и как предлагается:

```text
сейчас
date=2026-10-02T00:00:00.000Z | id_traf=1 | id_blog=1 | creo_name=Bahrain, USA, Canada, Israel / Pavel / 28.09 | budget=1.7 | chats=3 | subs=1

новый вид
stat_date=2026-10-02 | funnel_code=funnel_2 | buyer_id=1 | buyer_name=NEW_Pavel
source_code=facebook | placement_name=РАМИ_ФБ | creative_kind=ad_campaign
creative_name=Bahrain, USA, Canada, Israel / Pavel / 28.09
spend_usd=1.70 | spend_status=present | card_chats=3 | card_subscribers=1
```

`creative_kind` на первых порах можно ставить так: в имени есть куски стран и дата (`USA`, `28.09`, `Pavel`) → `ad_campaign`; имя как ник канала (`Whale200`, `BEFXTRADING`) → `tg_channel`. Это подпись для глаза. На неё нельзя вешать склейку денег и стартов, пока нет домена или id кампании.

### `channel_day` (было `buyer_stats_today_start_sub`)

| Было | Стало |
|---|---|
| `date` | `stat_date` |
| `creo_name` | `creative_name` |
| `count_start` | `starts` |
| `count_sub` | `subscribers` |
| `count_reg` | `registrations` |
| `count_ftd` | `ftds` |

Новые столбцы, все можно оставить пустыми:

| Столбец | Зачем |
|---|---|
| `funnel_code` | чтобы выгрузка не смешалась со второй базой |
| `buyer_id`, `buyer_name` | сейчас байера в ленте нет |
| `source_code`, `placement_name` | сейчас источника нет |
| `creative_kind` | для этой ленты почти везде `tg_channel` |

Пока `buyer_id` пустой, строка честно выглядит как «канал за день», а не как байер. С сентября эта лента почти одинаковая в обеих базах, поэтому `funnel_code` здесь особенно нужен: видно, что строка продублирована, а не посчитана дважды разными людьми.

### `buyer_day` (было `traffers_stat`)

| Было | Стало |
|---|---|
| `date` | `stat_date` |
| `traffer_name` | `buyer_name` |
| `count_start` | `starts` |
| `count_sub` | `subscribers` |
| `count_chat` | `chats` |
| `count_reg` | `registrations` |
| `count_ftd` | `ftds` |

Новые столбцы:

| Столбец | Откуда |
|---|---|
| `funnel_code` | константа |
| `buyer_id` | найти по `buyer_name` в `buyers` |

Креатива и источника здесь нет и не добавляем: это итог байера за день, не кампания.

### `project_day` (было `common_stats_report`)

| Было | Стало |
|---|---|
| `date` | `stat_date` |
| `chats` | `chats` |
| `regs` | `registrations` |
| `ftd` | `ftds` |
| `deps` | `deposits_count` |
| `sum_deps` | `deposits_sum_usd` |
| `sum_ftd` | `ftd_sum_usd` |
| `withd_count` | `withdrawals_count` |
| `sum_withd` | `withdrawals_sum_usd` |
| `com_count` | `commission_count` |
| `sum_com` | `commission_usd` |
| `sum_com_partners` | `partner_commission_usd` |

Новый столбец: `funnel_code`.

`partner_commission_usd` есть только в старой базе. В новой колонку тоже завести пустой, чтобы шапка совпала. Смысл этой цифры ещё не подтверждён: на 1 октября 2026 она равна комиссии новой воронки.

### `traders` (было `users_stat`)

| Было | Стало |
|---|---|
| `uid` | `trader_uid` |
| `tg_id` | `telegram_id` |
| `reg_date` | `registered_on` |
| `ftd_date` | `ftd_on` |
| `ftd_sum` | `ftd_sum_usd` |
| `deps` | `deposits_sum_usd` |
| `deps_count` | `deposits_count` |
| `withdrawals` | `withdrawals_sum_usd` |
| `withdrawals_count` | `withdrawals_count` |
| `balance` | `balance_usd` |
| `activity_date` | `last_active_on` |
| `country` | сырьё; рядом новый `country_code` |
| `report`, `prev_report` | оставляем как текст статуса |

Новые столбцы, пока пустые:

| Столбец | Зачем |
|---|---|
| `funnel_code` | трейдеры двух баз не пересекаются, подпись это фиксирует |
| `country_code` | `SA` вместо смеси `Saudi Arabia` / `SY` |
| `buyer_id`, `buyer_name` | когортный FTD байера; данных ещё нет |
| `source_code` | откуда пришёл |
| `creative_name` | какой креатив довёл |

Пустые столбцы как раз делают дырку видимой: у трейдера есть депозит и нет байера.

### `channel_country_day` (было `buyer_stats_today_start_sub_country`)

| Было | Стало |
|---|---|
| `date` | `stat_date` |
| `creo_name` | `creative_name` |
| `country` | `country_code` |
| `count_reg` | `registrations` |
| `count_ftd` | `ftds` |

Новые, по той же логике, что у `channel_day`: `funnel_code`, `buyer_id`, `buyer_name`, `source_code`, `placement_name`, `country_name`. Байера и источник не выдумываем, если имя креатива в этот день встречалось у двух людей.

### Гео-расход и выручка

`country_spend_day` (было `countries`):

| Было | Стало |
|---|---|
| `date` | `stat_date` |
| `id_traf` | `buyer_id` |
| `id_blog` | `placement_id` |
| `country_name` | `country_name` |
| `spend` | `spend_usd` |
| `month`, `year`, `week` | не показываем, есть дата |
| `pdp` | не показываем, пока нет расшифровки |

Новые: `funnel_code`, `buyer_name`, `source_code`, `placement_name`, `country_code`, `spend_status`.

`country_spend_week` и `country_spend_month`:

| Было | Стало |
|---|---|
| `ids_traf` | не показываем как id байера |
| `id_blog` | `placement_id` |
| `blogger` | `placement_name` |
| `country_name` | `country_name` |
| `spend` | `spend_usd` |
| `week` / `month` / `year` | `week_start` или `month_start` одной датой |

Новые: `funnel_code`, `brand_code`, `source_code`, `country_code`, `buyer_pool`.

`buyer_pool` — это нынешний текст `0,1,2,3` или `10,12,18,19,21,22,8`. Его нельзя назвать `buyer_id`: это список кабинетов пула, расход между ними не разделён. Отдельное имя это показывает.

`country_revenue_week` и `country_revenue_month`:

| Было | Стало |
|---|---|
| `id_blog` | `placement_id` |
| `country` | `country_name` |
| `registrations` | `registrations` |
| `comission` | `commission_usd` |
| `num_of_first_deps` | `ftds` |
| `sum_of_first_deps` | `ftd_sum_usd` |
| `num_of_deps` | `deposits_count` |
| `sum_of_deps` | `deposits_sum_usd` |
| `withdrawals` | `withdrawals_sum_usd` |
| `week`+`year` / `month`+`year` | `week_start` / `month_start` |
| `period` | `period_label` |

Новые: `funnel_code`, `brand_code`, `source_code`, `country_code`. `tier` не показываем, колонка пустая. Байера здесь нет, столбец `buyer_id` не добавляем, чтобы не казалось, что выручка уже его.

### Когорты

`cohort_week` / `cohort_month`:

| Было | Стало |
|---|---|
| `country` | `country_name` |
| `id_blog` | `placement_id` |
| `start_week`, `start_year` | `cohort_start` |
| `cur_week`, `cur_year` | `cohort_current` |
| `comission` | `commission_usd` |
| `ftd_count` | `ftds` |
| `reg_count` | `registrations` |
| `deps` | `deposits_sum_usd` |
| `start_period`, `cur_period` | `cohort_start_label`, `cohort_current_label` |

Новые: `funnel_code`, `source_code`, `country_code`.

Байера в когорте нет. Отдельный `buyer_id` не рисуем.

### `kpi_targets` (было `buyers_kpi`, только старая база)

| Было | Стало |
|---|---|
| `id_traf` | `buyer_id` |
| `id_blog` | `placement_id` |
| `country` | `country_name` |
| `cost_ftd` | `max_cpa_ftd_usd` |
| `cost_reg` | `max_cpa_registration_usd` |
| `cost_start` | `max_cost_start_usd` |
| `cost_sub` | `max_cost_subscriber_usd` |
| `cost_ls` | `max_cost_chat_usd` |

Новые: `funnel_code`, `buyer_name`, `source_code`, `country_code`.

Та же пустая шапка нужна и в новой базе: порогов там сейчас нет, таблица из нуля строк всё равно показывает, какие нормы вообще бывают.

### `project_spend_day` и `load_status`

`spend_fact`: `date` → `stat_date`, `spend` → `spend_usd`, плюс `funnel_code`. Байера нет, это сумма проекта.

`update_info`: `id_traf` → `buyer_id`, `timestamp_creo` → `creative_loaded_at`, `timestamp_country` → `country_loaded_at`, плюс `buyer_name` и `funnel_code`.

## Что завести даже пустым

Эти столбцы одинаковы в обеих базах. Пустая ячейка здесь полезна: видно, чего данные ещё не умеют.

| Где | Столбец | Что будет, когда появится |
|---|---|---|
| `channel_day`, `channel_country_day` | `buyer_id` | лента канала перестанет быть безымянной |
| `traders` | `buyer_id`, `source_code`, `creative_name` | когортный FTD конкретного байера |
| `creative_day` | `domain` | домен, который выдали байеру; мост между кабинетом и инвайтером |
| `creative_day` | `campaign_id` | настоящий id кампании вместо пустого `id_creo` |
| `buyers` | `person_code` | один человек на две воронки |

`domain` и `campaign_id` в сырых данных сейчас нет. Колонки стоит показать в макете и не заполнять заглушкой.

## Что сознательно не делаем в этом шаге

- Не сливаем две схемы в одну: одинаковые `buyer_id` означают разных людей.
- Не копируем ленту каналов из одной базы в другую ещё раз. Она и так почти общая с сентября.
- Не раскладываем `buyer_pool` (`ids_traf` списком) на несколько байеров.
- Не подставляем ноль вместо пустого расхода. Пусто остаётся пустым, `spend_status=missing`.
- Не удаляем старые столбцы, пока в них пишет загрузчик.

## Как это включить

1. В каждой схеме вьюхи с новыми именами поверх старых таблиц. Бот и человек читают вьюху.
2. Загрузчик по-прежнему пишет `creos`, `traffers`, `bloggers`.
3. После сверки с Андреем те же имена можно сделать физическими таблицами и перевести загрузчик.

До этой сверки предложение — макет нейминга, не миграция.
