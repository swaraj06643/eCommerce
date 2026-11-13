var updateBtns = document.getElementsByClassName('update-cart');

function showCartToast() {
    var toast = document.getElementById('cart-toast');
    if (!toast) return;
    toast.classList.add('visible');
    clearTimeout(showCartToast.timer);
    showCartToast.timer = setTimeout(function () {
        toast.classList.remove('visible');
    }, 2600);
}

function cartLineKey(productId, variantId) {
    return variantId ? productId + ':' + variantId : String(productId);
}

function currentCartQuantity(key) {
    return cart[key] ? Number(cart[key].quantity) || 0 : 0;
}

for (var i = 0; i < updateBtns.length; i++) {
    updateBtns[i].addEventListener('click', async function () {
        if (this.disabled) return;

        var button = this;
        var productId = button.dataset.product;
        var action = button.dataset.action;
        var afterAdd = button.dataset.afterAdd;
        var variantSelector = document.getElementById('product-variant');
        var isDetailPurchase = button.closest('.detail-actions');
        var variantId = isDetailPurchase && variantSelector
            ? variantSelector.value
            : (button.dataset.variantId || '');

        if (isDetailPurchase && variantSelector && !variantId) {
            variantSelector.reportValidity();
            return;
        }

        var key = cartLineKey(productId, variantId);
        var stock = isDetailPurchase && variantSelector && variantId
            ? variantSelector.selectedOptions[0].dataset.stock
            : button.dataset.stock;

        if (action === 'add' && stock !== undefined && stock !== '' &&
            currentCartQuantity(key) >= Number(stock)) {
            window.alert('There is no more stock available for this product.');
            return;
        }

        if (button.tagName === 'BUTTON') button.disabled = true;
        try {
            if (user === 'AnonymousUser') {
                if (action === 'add') {
                    if (!cart[key]) cart[key] = { quantity: 0 };
                    cart[key].quantity += 1;
                } else if (action === 'remove') {
                    if (!cart[key]) return;
                    cart[key].quantity -= 1;
                    if (cart[key].quantity <= 0) delete cart[key];
                }
                document.cookie = 'cart=' + encodeURIComponent(JSON.stringify(cart)) + ';path=/;SameSite=Lax';
            } else {
                var response = await fetch('/update_item/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrftoken || '',
                    },
                    credentials: 'same-origin',
                    body: JSON.stringify({
                        productId: productId,
                        variantId: variantId,
                        action: action,
                    }),
                });
                var result = await response.json();
                if (!response.ok) throw new Error(result.error || 'The server could not update your bag.');
            }

            if (action === 'add') showCartToast();
            if (afterAdd) {
                window.location.assign(afterAdd);
            } else {
                window.location.reload();
            }
        } catch (error) {
            console.error('Could not update the bag.', error);
            window.alert(error.message || 'We could not update your bag. Please try again.');
            if (button.tagName === 'BUTTON') button.disabled = false;
        }
    });
}
