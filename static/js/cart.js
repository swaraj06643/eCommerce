var updateBtns = document.getElementsByClassName('update-cart')

function showCartToast() {
	var toast = document.getElementById('cart-toast')
	if (!toast) return;
	toast.classList.add('visible')
	clearTimeout(showCartToast.timer)
	showCartToast.timer = setTimeout(function(){
		toast.classList.remove('visible')
	}, 2600)
}

for (i = 0; i < updateBtns.length; i++) {
	updateBtns[i].addEventListener('click', function(){
		var productId = this.dataset.product
		var action = this.dataset.action
		var afterAdd = this.dataset.afterAdd
		var variantSelector = document.getElementById('product-variant')
		var variantId = variantSelector ? variantSelector.value : (this.dataset.variantId || '')
		if (variantSelector && !variantId) {
			variantSelector.reportValidity()
			return
		}
		console.log('productId:', productId, 'Action:', action)
		console.log('USER:', user)

		if (action == 'add') {
			showCartToast()
		}

		if (user == 'AnonymousUser'){
			addCookieItem(productId, action, afterAdd, variantId)
		}else{
			updateUserOrder(productId, action, afterAdd, variantId)
		}
	})
}

function addCookieItem(productId, action, afterAdd, variantId){
	console.log('User is not authenticated')
	var cartKey = variantId ? productId + ':' + variantId : productId

	if (action == 'add'){
		if (cart[cartKey] == undefined){
		cart[cartKey] = {'quantity':1}

		}else{
			cart[cartKey]['quantity'] += 1
		}
	}

	if (action == 'remove'){
		if (cart[cartKey] == undefined) return
		cart[cartKey]['quantity'] -= 1

		if (cart[cartKey]['quantity'] <= 0){
			console.log('Item should be deleted')
			delete cart[cartKey];
		}
	}
	console.log('CART:', cart)
	document.cookie ='cart=' + JSON.stringify(cart) + ";domain=;path=/"

	if (afterAdd) {
		location.assign(afterAdd)
	} else {
		location.reload()
	}
}

function updateUserOrder(productId, action, afterAdd, variantId){
	console.log('User is authenticated, sending data...')

		var url = '/update_item/'

		fetch(url, {
			method:'POST',
			headers:{
				'Content-Type':'application/json',
				'X-CSRFToken':csrftoken,
			},
			body:JSON.stringify({'productId':productId, 'variantId':variantId, 'action':action})
		})
		.then((response) => {
		   if (!response.ok) {
		       throw new Error('The server could not update your bag.')
		   }
		   return response.json();
		})
		.then((data) => {
		    if (afterAdd) {
		        location.assign(afterAdd)
		    } else {
		        location.reload()
		    }
		})
		.catch((error) => {
		    console.error('Could not update the bag.', error)
		    window.alert('We could not update your bag. Please try again.')
		});
}
