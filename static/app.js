/* Поведение страниц панели без инлайн-обработчиков (CSP script-src 'self').
   Все обработчики делегированные: работают и для контента, подменённого HTMX. */
(function () {
    'use strict';

    /* Модалка заявки: [data-modal-open] открывает, [data-modal-close] закрывает. */
    document.addEventListener('click', function (event) {
        var opener = event.target.closest('[data-modal-open]');
        if (opener) {
            var dialog = document.getElementById('ticket-modal');
            if (dialog && typeof dialog.showModal === 'function') {
                dialog.showModal();
            }
            return;
        }
        var closer = event.target.closest('[data-modal-close]');
        if (closer) {
            var openDialog = document.getElementById('ticket-modal');
            if (openDialog) {
                openDialog.close();
            }
        }
    });

    /* Подтверждения: форма с data-confirm спрашивает перед отправкой.
       Тексты приходят из шаблона уже на языке страницы; window.confirm показывает
       их как простой текст — XSS-поверхности нет. */
    var RU = document.documentElement.lang !== 'en';
    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-confirm')) {
            return;
        }
        var message = form.getAttribute('data-confirm') || (RU ? 'Вы уверены?' : 'Are you sure?');
        if (!window.confirm(message)) {
            event.preventDefault();
        }
    }, true);

    /* Формы с галочками очистки ключей (админка): предупреждение, если что-то отмечено. */
    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-confirm-clear')) {
            return;
        }
        if (form.querySelector('input[type="checkbox"]:checked')) {
            if (!window.confirm(form.getAttribute('data-confirm-clear') || (RU ? 'Продолжить?' : 'Continue?'))) {
                event.preventDefault();
            }
        }
    }, true);

    /* Глазок пароля: [data-toggle-password="id"] переключает тип поля. */
    document.addEventListener('click', function (event) {
        var toggle = event.target.closest('[data-toggle-password]');
        if (!toggle) {
            return;
        }
        var input = document.getElementById(toggle.getAttribute('data-toggle-password'));
        if (input) {
            input.type = input.type === 'password' ? 'text' : 'password';
        }
    });

    /* Шаблоны заявок: кнопка [data-template-fill] заполняет заголовок и описание. */
    document.addEventListener('click', function (event) {
        var button = event.target.closest('[data-template-fill]');
        if (!button) {
            return;
        }
        var title = document.getElementById('title');
        var description = document.getElementById('description');
        if (title) {
            title.value = button.getAttribute('data-title') || '';
        }
        if (description) {
            description.value = button.getAttribute('data-description') || '';
        }
        /* Подстановка {…}: выделить первую — в заголовке, иначе в описании,
           чтобы оператор сразу напечатал номер базы поверх. */
        [title, description].some(function (field) {
            var match = field ? /\{[^{}]*\}/.exec(field.value) : null;
            if (!match) {
                return false;
            }
            field.focus();
            field.setSelectionRange(match.index, match.index + match[0].length);
            return true;
        });
    });

    /* Строка «Заявка №N закрыта» — одноразовая: после показа убрать done/n из адреса,
       чтобы обновление страницы её не повторяло. */
    if (window.history && window.history.replaceState && /[?&]done=/.test(window.location.search)) {
        var params = new URLSearchParams(window.location.search);
        params.delete('done');
        params.delete('n');
        var query = params.toString();
        window.history.replaceState(null, '', window.location.pathname + (query ? '?' + query : ''));
    }

    /* «Закрыть» на карточке: при раскрытии — курсор в поле комментария. */
    document.addEventListener('toggle', function (event) {
        var box = event.target;
        if (!(box instanceof HTMLDetailsElement) || !box.classList.contains('close-box') || !box.open) {
            return;
        }
        var card = box.closest('.card');
        var field = card ? card.querySelector('.close-form input[name="comment"]') : null;
        if (field) {
            field.focus();
        }
    }, true);

    /* Чипы комментария закрытия: [data-comment-fill] пишет текст в поле своей формы. */
    document.addEventListener('click', function (event) {
        var chip = event.target.closest('[data-comment-fill]');
        if (!chip) {
            return;
        }
        var form = chip.closest('form');
        var field = form ? form.querySelector('input[name="comment"]') : null;
        if (field) {
            field.value = chip.getAttribute('data-comment-fill') || '';
            field.focus();
        }
    });

    /* Быстрое время: [data-planned-preset="night|tonight|tomorrow|none"] заполняет виджет.
       Считается по МСК (UTC+3), а не по часовому поясу браузера. */
    function mskParts(offsetDays) {
        var msk = new Date(Date.now() + 3 * 3600 * 1000 + (offsetDays || 0) * 86400 * 1000);
        var pad = function (n) { return n < 10 ? '0' + n : String(n); };
        return {
            date: msk.getUTCFullYear() + '-' + pad(msk.getUTCMonth() + 1) + '-' + pad(msk.getUTCDate()),
            hour: msk.getUTCHours()
        };
    }

    document.addEventListener('click', function (event) {
        var preset = event.target.closest('[data-planned-preset]');
        if (!preset) {
            return;
        }
        var widget = document.getElementById('planned-widget');
        if (!widget) {
            return;
        }
        var date = widget.querySelector('#planned_date');
        var from = widget.querySelector('#planned_time_from');
        var to = widget.querySelector('#planned_time_to');
        var exact = widget.querySelector('input[name="planned_mode"][value="exact"]');
        var kind = preset.getAttribute('data-planned-preset');
        var now = mskParts(0);
        var value = { date: '', time: '' };
        if (kind === 'night') {
            /* ближайшие 02:00 МСК: до 02:00 — сегодня, иначе завтра */
            value = { date: now.hour < 2 ? now.date : mskParts(1).date, time: '02:00' };
        } else if (kind === 'tonight') {
            value = { date: now.date, time: '23:00' };
        } else if (kind === 'tomorrow') {
            value = { date: mskParts(1).date, time: '10:00' };
        }
        if (exact) {
            exact.checked = true;
            exact.dispatchEvent(new Event('change', { bubbles: true }));
        }
        if (date) { date.value = value.date; }
        if (from) { from.value = value.time; }
        if (to) { to.value = ''; }
    });

    /* Хоткеи: «/» — поиск, N — новая заявка (вне полей ввода),
       Ctrl/Cmd+Enter — отправить форму, в которой стоит курсор. */
    document.addEventListener('keydown', function (event) {
        var target = event.target;
        var typing = target instanceof HTMLElement &&
            (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && typing) {
            var form = target.closest('form[data-hotkey-submit]');
            if (form) {
                event.preventDefault();
                form.requestSubmit();
            }
            return;
        }
        if (typing || event.ctrlKey || event.metaKey || event.altKey) {
            return;
        }
        if (event.key === '/') {
            var search = document.querySelector('[data-hotkey-search]');
            if (search) {
                event.preventDefault();
                search.focus();
                search.select();
            }
        } else if (event.key === 'n' || event.key === 'N' || event.key === 'т' || event.key === 'Т') {
            if (document.querySelector('[data-hotkey-new]')) {
                event.preventDefault();
                window.location.href = '/tickets/new';
            }
        }
    });

    /* Виджет планового времени: режим «Диапазон» показывает второе время
       и меняет подпись первого. Инициализация по наличию #planned-widget. */
    function bindPlannedWidget(widget) {
        if (!widget || widget.getAttribute('data-planned-bound') === '1') {
            return;
        }
        widget.setAttribute('data-planned-bound', '1');
        var toBlock = widget.querySelector('[data-planned-to]');
        var fromLabel = widget.querySelector('[data-planned-from-label]');
        var sync = function () {
            var mode = widget.querySelector('input[name="planned_mode"]:checked');
            var range = mode !== null && mode.value === 'range';
            if (toBlock) {
                toBlock.style.display = range ? '' : 'none';
            }
            if (fromLabel) {
                fromLabel.textContent = range
                    ? fromLabel.getAttribute('data-label-from') || 'С'
                    : fromLabel.getAttribute('data-label-time') || 'Время';
            }
        };
        widget.addEventListener('change', sync);
        sync();
    }

    var initPlannedWidgets = function () {
        document.querySelectorAll('#planned-widget').forEach(bindPlannedWidget);
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initPlannedWidgets);
    } else {
        initPlannedWidgets();
    }
})();
