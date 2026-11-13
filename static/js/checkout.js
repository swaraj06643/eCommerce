(() => {
    const form = document.getElementById('checkout-form');
    const pin = document.getElementById('checkout-pincode');
    const button = document.getElementById('checkout-postal-check');
    const message = document.getElementById('checkout-postal-message');
    const coinInput = document.getElementById('coins-to-redeem');
    const totalOutput = document.getElementById('checkout-total-preview');
    const coinDiscount = document.getElementById('coin-discount-preview');

    if (coinInput && totalOutput) {
        const baseTotal = Number(totalOutput.dataset.baseTotal || 0);
        const updateCoinPreview = () => {
            const coins = Math.max(0, Number.parseInt(coinInput.value, 10) || 0);
            const validCoins = coins % 100 === 0 && coins <= Number(coinInput.max || 0);
            const savings = validCoins ? Math.min((coins / 100) * 10, baseTotal) : 0;
            totalOutput.textContent = `Total to pay: ₹${(baseTotal - savings).toFixed(2)}`;
            if (coinDiscount) coinDiscount.textContent = savings.toFixed(2);
        };
        coinInput.addEventListener('input', updateCoinPreview);
        updateCoinPreview();
    }

    if (!form || !pin || !button || !message) {
        return;
    }

    const city = form.querySelector('[name="city"]');
    const state = form.querySelector('[name="state"]');
    const address = form.querySelector('[name="address"]');
    const phone = form.querySelector('[name="phone"]');
    const savedAddressSelect = form.querySelector('[name="saved_address_id"]');
    const saveAddressCheck = document.getElementById('save-address-check');
    const savedAddressLabel = document.getElementById('saved-address-label-field');
    const csrfToken = form.querySelector('[name="csrfmiddlewaretoken"]').value;

    if (savedAddressSelect) {
        savedAddressSelect.addEventListener('change', () => {
            const selected = savedAddressSelect.selectedOptions[0];
            if (!savedAddressSelect.value) {
                [address, city, state, pin, phone].forEach((field) => {
                    if (field) field.value = '';
                });
                return;
            }
            address.value = selected.dataset.address || '';
            city.value = selected.dataset.city || '';
            state.value = selected.dataset.state || '';
            pin.value = selected.dataset.zipcode || '';
            phone.value = selected.dataset.phone || phone.value;
        });
        [address, city, state, pin, phone].forEach((field) => {
            if (field) {
                field.addEventListener('input', () => {
                    if (savedAddressSelect.value) savedAddressSelect.value = '';
                });
                field.addEventListener('change', () => {
                    if (savedAddressSelect.value) savedAddressSelect.value = '';
                });
            }
        });
    }

    if (saveAddressCheck && savedAddressLabel) {
        saveAddressCheck.addEventListener('change', () => {
            savedAddressLabel.hidden = !saveAddressCheck.checked;
        });
    }

    button.addEventListener('click', async () => {
        if (!pin.reportValidity()) {
            return;
        }

        message.textContent = 'Checking the Indian postal directory...';
        message.classList.remove('delivery-message-success');
        button.disabled = true;
        const payload = { pincode: pin.value };
        if (city.value.trim() && state.value) {
            payload.city = city.value.trim();
            payload.state = state.value;
        }

        try {
            const response = await fetch(form.dataset.checkUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                },
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.message || 'Could not verify this PIN code.');
            }

            if (result.locations && result.locations.length) {
                const location = result.locations[0];
                if (!city.value.trim()) {
                    city.value = location.district || location.locality;
                }
                if (!state.value) {
                    state.value = location.state;
                }
            }
            message.textContent = result.message;
            message.classList.add('delivery-message-success');
        } catch (error) {
            message.textContent = error.message || 'Could not reach the postal directory. Please try again.';
        } finally {
            button.disabled = false;
        }
    });

    pin.addEventListener('input', () => {
        message.classList.remove('delivery-message-success');
    });
})();
