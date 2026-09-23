/* Mini Search - public search page behaviour (vanilla JS, no dependencies) */
(function () {
    'use strict';

    /* ---------------- Filters ---------------- */
    function initFilters() {
        var input = document.getElementById('filterInput');
        var form = document.getElementById('searchForm');
        if (!input || !form) return;

        document.querySelectorAll('[data-filter]').forEach(function (button) {
            button.addEventListener('click', function () {
                input.value = button.dataset.filter;
                form.submit();
            });
        });
    }

    /* ---------------- Sorting (client-side, already-loaded results) ---------------- */
    function initSorting() {
        var select = document.getElementById('sortSelect');
        var list = document.getElementById('resultList');
        if (!select || !list) return;

        var original = Array.prototype.slice.call(list.children);

        select.addEventListener('change', function () {
            var mode = select.value;
            var items = Array.prototype.slice.call(list.children);

            if (mode === 'relevance') {
                items = original.slice();
            } else if (mode === 'title') {
                items.sort(function (a, b) {
                    return (a.dataset.title || '').localeCompare(b.dataset.title || '', 'cs');
                });
            } else if (mode === 'date') {
                items.sort(function (a, b) {
                    return Number(b.dataset.date || 0) - Number(a.dataset.date || 0);
                });
            }

            items.forEach(function (item) { list.appendChild(item); });
        });
    }

    /* ---------------- Autocomplete ---------------- */
    function initAutocomplete() {
        var input = document.getElementById('searchInput');
        var dropdown = document.getElementById('acDropdown');
        if (!input || !dropdown) return;

        var timer = null;
        var items = [];
        var activeIndex = -1;

        function close() {
            dropdown.classList.remove('open');
            dropdown.innerHTML = '';
            items = [];
            activeIndex = -1;
        }

        function highlight(index) {
            var children = dropdown.querySelectorAll('.ac-item');
            children.forEach(function (child, i) {
                child.classList.toggle('highlighted', i === index);
            });
            activeIndex = index;
        }

        function render(suggestions) {
            dropdown.innerHTML = '';
            items = suggestions;
            if (!suggestions.length) { close(); return; }

            suggestions.forEach(function (text, index) {
                var row = document.createElement('div');
                row.className = 'ac-item';
                row.dataset.value = text;
                row.setAttribute('role', 'option');

                var icon = document.createElement('span');
                icon.className = 'ac-icon';
                icon.textContent = '\u25CB';

                var label = document.createElement('span');
                label.textContent = text;

                row.appendChild(icon);
                row.appendChild(label);
                row.addEventListener('mousedown', function (event) {
                    event.preventDefault();
                    choose(text);
                });
                row.addEventListener('mouseenter', function () { highlight(index); });
                dropdown.appendChild(row);
            });

            activeIndex = -1;
            dropdown.classList.add('open');
        }

        function choose(value) {
            input.value = value;
            close();
            input.form.submit();
        }

        function fetchSuggestions(query) {
            fetch('/autocomplete?q=' + encodeURIComponent(query))
                .then(function (response) { return response.json(); })
                .then(function (data) { render(data.results || []); })
                .catch(function () { close(); });
        }

        input.addEventListener('input', function () {
            var query = input.value.trim();
            clearTimeout(timer);
            if (query.length < 2) { close(); return; }
            timer = setTimeout(function () { fetchSuggestions(query); }, 280);
        });

        input.addEventListener('keydown', function (event) {
            if (!dropdown.classList.contains('open')) return;
            if (event.key === 'ArrowDown') {
                event.preventDefault();
                highlight(Math.min(activeIndex + 1, items.length - 1));
            } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                highlight(Math.max(activeIndex - 1, 0));
            } else if (event.key === 'Enter' && activeIndex >= 0) {
                event.preventDefault();
                choose(items[activeIndex]);
            } else if (event.key === 'Escape') {
                close();
            }
        });

        document.addEventListener('click', function (event) {
            if (!event.target.closest('.search-wrap')) close();
        });
    }

    /* ---------------- Result image fallback ---------------- */
    function initThumbs() {
        document.querySelectorAll('.result-thumb').forEach(function (image) {
            image.addEventListener('error', function () { image.style.display = 'none'; });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initFilters();
        initSorting();
        initAutocomplete();
        initThumbs();
    });
})();