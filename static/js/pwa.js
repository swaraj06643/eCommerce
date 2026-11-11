(function () {
    var installButton = document.getElementById('install-app-button');
    var installPrompt = null;
    var standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/service-worker.js').catch(function () {});
        });
    }

    if (!installButton || standalone) {
        if (installButton) installButton.hidden = true;
        return;
    }

    window.addEventListener('beforeinstallprompt', function (event) {
        event.preventDefault();
        installPrompt = event;
        installButton.hidden = false;
    });

    window.addEventListener('appinstalled', function () {
        installPrompt = null;
        installButton.hidden = true;
    });

    installButton.addEventListener('click', async function () {
        if (!installPrompt) {
            window.alert('To install, open your browser menu and choose “Install app” or “Add to Home Screen”.');
            return;
        }
        installPrompt.prompt();
        await installPrompt.userChoice;
        installPrompt = null;
        installButton.hidden = true;
    });
}());
