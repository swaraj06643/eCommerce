(function () {
    var root = document.documentElement;

    function applyTheme(theme) {
        var isDark = theme === 'dark';
        root.dataset.theme = isDark ? 'dark' : 'light';
        document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
            button.setAttribute('aria-pressed', String(isDark));
            button.setAttribute('aria-label', isDark ? 'Switch to light theme' : 'Switch to dark theme');
            var label = button.querySelector('[data-theme-label]');
            if (label) label.textContent = isDark ? 'Light mode' : 'Dark mode';
            var icon = button.querySelector('[data-theme-icon]');
            if (icon) icon.textContent = isDark ? '☀' : '◐';
        });
        var themeColor = document.querySelector('meta[name="theme-color"]');
        if (themeColor) themeColor.setAttribute('content', isDark ? '#151a21' : '#e85f54');
    }

    applyTheme(root.dataset.theme || 'light');
    document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
        button.addEventListener('click', function () {
            var nextTheme = root.dataset.theme === 'dark' ? 'light' : 'dark';
            try {
                localStorage.setItem('avatar-theme', nextTheme);
            } catch (error) {}
            applyTheme(nextTheme);
        });
    });
}());
