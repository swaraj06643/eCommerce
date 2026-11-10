(() => {
    const form = document.getElementById('delivery-check');
    const pin = document.getElementById('delivery-pin');
    const message = document.getElementById('delivery-message');
    const variantSelect = document.getElementById('product-variant');
    const price = document.querySelector('.detail-price');
    const listPrice = document.querySelector('.detail-list-price');
    const discount = document.querySelector('.detail-discount');
    const stockMessage = document.getElementById('detail-stock-message');
    const purchaseButtons = document.querySelectorAll('.detail-actions .update-cart');

    if (form && pin && message) {
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) {
                return;
            }

            message.textContent = 'Checking the Indian postal directory...';
            message.classList.remove('delivery-message-success');
            const button = form.querySelector('button[type="submit"]');
            button.disabled = true;
            try {
                const response = await fetch(form.dataset.checkUrl, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': form.querySelector('[name="csrfmiddlewaretoken"]').value,
                    },
                    body: JSON.stringify({ pincode: pin.value }),
                });
                const result = await response.json();
                message.textContent = result.message;
                if (!response.ok) {
                    throw new Error(result.message || 'Could not verify this PIN code.');
                }
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
    }

    if (variantSelect && price && stockMessage) {
        variantSelect.addEventListener('change', () => {
            const option = variantSelect.selectedOptions[0];
            if (!option.value) {
                const basePrice = Number(price.dataset.basePrice);
                const listPriceValue = Number(price.dataset.listPrice);
                price.textContent = `₹${basePrice.toFixed(2)}`;
                if (listPrice && discount && listPriceValue > basePrice) {
                    listPrice.textContent = `₹${listPriceValue.toFixed(2)}`;
                    listPrice.hidden = false;
                    discount.textContent = `${Math.round((listPriceValue - basePrice) / listPriceValue * 100)}% off`;
                    discount.hidden = false;
                }
                stockMessage.textContent = 'Choose an option to see its stock and pricing.';
                purchaseButtons.forEach((button) => { button.disabled = false; });
                return;
            }

            const basePrice = Number(price.dataset.basePrice);
            const adjustment = Number(option.dataset.adjustment || 0);
            const currentPrice = basePrice + adjustment;
            price.textContent = `₹${currentPrice.toFixed(2)}`;
            const listPriceValue = Number(price.dataset.listPrice);
            if (listPrice && discount) {
                if (listPriceValue > currentPrice) {
                    listPrice.textContent = `₹${listPriceValue.toFixed(2)}`;
                    listPrice.hidden = false;
                    discount.textContent = `${Math.round((listPriceValue - currentPrice) / listPriceValue * 100)}% off`;
                    discount.hidden = false;
                } else {
                    listPrice.hidden = true;
                    discount.hidden = true;
                }
            }
            const stock = option.dataset.stock;
            if (stock === '') {
                stockMessage.textContent = 'Stock availability is confirmed when you place your order.';
                purchaseButtons.forEach((button) => { button.disabled = false; });
            } else if (Number(stock) === 0) {
                stockMessage.textContent = 'This option is currently out of stock.';
                purchaseButtons.forEach((button) => { button.disabled = true; });
            } else {
                stockMessage.textContent = `${stock} in stock for this option.`;
                purchaseButtons.forEach((button) => { button.disabled = false; });
            }
        });
    }
})();
