(() => {
    function setupCancellationReasons() {
        document.querySelectorAll('.cancel-reason-select').forEach((select) => {
            const detail = select.form.querySelector('.cancel-reason-detail');
            if (!detail) return;

            const syncDetail = () => {
                const requiresDetail = select.value === 'other';
                detail.hidden = !requiresDetail;
                detail.required = requiresDetail;
            };
            select.addEventListener('change', syncDetail);
            syncDetail();
        });
    }

    function updateFulfillment(container, fulfillment) {
        const card = container.querySelector('[data-fulfillment-id="' + Number(fulfillment.id) + '"]');
        if (!card) return;

        const status = card.querySelector('[data-fulfillment-status]');
        if (status) status.textContent = fulfillment.status;

        const updated = card.querySelector('[data-fulfillment-updated]');
        if (updated) {
            const date = new Date(fulfillment.updated_at);
            updated.dateTime = fulfillment.updated_at;
            updated.textContent = Number.isNaN(date.getTime()) ? fulfillment.updated_at : date.toLocaleString();
        }

        const trackingDetails = card.querySelector('[data-tracking-details]');
        if (trackingDetails) {
            trackingDetails.textContent = [fulfillment.shipping_partner, fulfillment.tracking_number]
                .filter(Boolean)
                .join(' · ');
        }

        const trackingLink = card.querySelector('[data-tracking-link]');
        if (trackingLink) {
            try {
                const url = new URL(fulfillment.tracking_url, window.location.origin);
                if (url.protocol === 'https:' && fulfillment.tracking_url) {
                    trackingLink.href = url.href;
                    trackingLink.hidden = false;
                } else {
                    trackingLink.removeAttribute('href');
                    trackingLink.hidden = true;
                }
            } catch (error) {
                trackingLink.removeAttribute('href');
                trackingLink.hidden = true;
            }
        }
    }

    async function refreshOrder(container) {
        try {
            const response = await fetch(container.dataset.orderStatusUrl, {
                credentials: 'same-origin',
                headers: { 'Accept': 'application/json' },
                cache: 'no-store',
            });
            if (!response.ok) return;
            const data = await response.json();
            const statusScope = container.closest('.order-card, .order-complete-page') || container;
            const orderStatus = statusScope.querySelector('[data-order-status-label]');
            if (orderStatus) orderStatus.textContent = data.order_status;
            (data.fulfillments || []).forEach((fulfillment) => {
                updateFulfillment(container, fulfillment);
            });
        } catch (error) {
            // Keep the last rendered order status when temporarily offline.
        }
    }

    setupCancellationReasons();
    const orderContainers = Array.from(document.querySelectorAll('[data-order-status-url]'));
    if (orderContainers.length) {
        const refresh = () => orderContainers.forEach(refreshOrder);
        refresh();
        window.setInterval(refresh, 8000);
    }
})();
