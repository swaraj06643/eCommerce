(() => {
    const splash = document.getElementById('avatar-splash');
    if (!splash) {
        return;
    }

    const dismissSplash = () => {
        splash.classList.add('is-dismissed');
        splash.setAttribute('aria-hidden', 'true');
    };

    try {
        if (window.sessionStorage.getItem('avatarSplashSeen') === 'true') {
            dismissSplash();
            return;
        }
        window.sessionStorage.setItem('avatarSplashSeen', 'true');
    } catch (error) {
        console.warn('Avatar splash could not access session storage.', error);
    }

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.setTimeout(dismissSplash, reducedMotion ? 500 : 2100);
})();
