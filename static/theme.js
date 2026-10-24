/* Темы панели: «Ночь» (new — джунгли, енот, бирюзовый акцент) и «Классика»
   (classic — светлый строгий вид).
   Тему рендерит сервер из личного профиля (data-theme на <html>), поэтому мигания нет.
   Клик по #theme-toggle (он есть, только если админ разрешил личный вид) сразу
   меняет атрибут и сохраняет выбор в профиль: POST /profile/appearance с CSRF-заголовком. */
(function () {
    'use strict';
    var ORDER = ['new', 'classic'];
    var EN = document.documentElement.lang === 'en';
    var LABELS = EN ? { new: 'Night', classic: 'Classic' } : { new: 'Ночь', classic: 'Классика' };

    function current() {
        var value = document.documentElement.getAttribute('data-theme');
        return ORDER.indexOf(value) === -1 ? 'classic' : value;
    }

    function label(theme) {
        var next = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
        document.querySelectorAll('#theme-toggle').forEach(function (button) {
            button.setAttribute('data-theme', theme);
            button.setAttribute('title', EN
                ? 'Theme: ' + LABELS[theme] + ' — switch to “' + LABELS[next] + '”'
                : 'Тема: ' + LABELS[theme] + ' — переключить на «' + LABELS[next] + '»');
            var text = button.querySelector('[data-theme-label]');
            if (text) {
                text.textContent = LABELS[theme];
            }
        });
    }

    function save(theme) {
        var meta = document.querySelector('meta[name="csrf-token"]');
        var body = new URLSearchParams({ theme: theme });
        return fetch('/profile/appearance', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-CSRF-Token': meta ? meta.getAttribute('content') : '',
                'X-Requested-With': 'fetch'
            },
            body: body.toString(),
            credentials: 'same-origin'
        });
    }

    label(current());

    document.addEventListener('click', function (event) {
        if (!event.target.closest('#theme-toggle')) {
            return;
        }
        var previous = current();
        var next = ORDER[(ORDER.indexOf(previous) + 1) % ORDER.length];
        document.documentElement.setAttribute('data-theme', next);
        label(next);
        save(next).then(function (response) {
            if (!response.ok) {
                document.documentElement.setAttribute('data-theme', previous);
                label(previous);
            }
        }).catch(function () {
            document.documentElement.setAttribute('data-theme', previous);
            label(previous);
        });
    });
})();
