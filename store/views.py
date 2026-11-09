import datetime
import json

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .models import *
from .utils import cartData, guestOrder


def get_or_create_customer_for_user(user):
    if not user or not user.is_authenticated:
        return None

    customer, _ = Customer.objects.get_or_create(user=user)
    if not customer.name and user.first_name:
        customer.name = user.get_full_name() or user.username
    if not customer.email and user.email:
        customer.email = user.email
    customer.save()
    return customer


def store(request):
    data = cartData(request)
    cartItems = data['cartItems']

    search_query = request.GET.get('q', '').strip()
    products = Product.objects.all()
    if search_query:
        products = products.filter(name__icontains=search_query)

    context = {'products': products, 'cartItems': cartItems, 'search_term': search_query}
    return render(request, 'store/store.html', context)


def cart(request):
    data = cartData(request)

    cartItems = data['cartItems']
    order = data['order']
    items = data['items']

    context = {'items': items, 'order': order, 'cartItems': cartItems}
    return render(request, 'store/cart.html', context)


def checkout(request):
    data = cartData(request)

    cartItems = data['cartItems']
    order = data['order']
    items = data['items']

    states = [
        'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar', 'Chhattisgarh', 'Goa', 'Gujarat',
        'Haryana', 'Himachal Pradesh', 'Jharkhand', 'Karnataka', 'Kerala', 'Madhya Pradesh',
        'Maharashtra', 'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland', 'Odisha', 'Punjab',
        'Rajasthan', 'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura', 'Uttar Pradesh',
        'Uttarakhand', 'West Bengal', 'Andaman and Nicobar Islands', 'Chandigarh',
        'Dadra and Nagar Haveli and Daman and Diu', 'Delhi', 'Jammu and Kashmir', 'Ladakh',
        'Lakshadweep', 'Puducherry'
    ]

    cities = [
        'Agra', 'Ahmedabad', 'Ajmer', 'Allahabad', 'Amritsar', 'Asansol', 'Aurangabad', 'Bengaluru',
        'Bhopal', 'Bhubaneswar', 'Bhilai', 'Chandigarh', 'Chennai', 'Coimbatore', 'Cuttack', 'Delhi',
        'Dehradun', 'Dhanbad', 'Durgapur', 'Faridabad', 'Ghaziabad', 'Goa', 'Gorakhpur', 'Guwahati',
        'Hyderabad', 'Indore', 'Jaipur', 'Jammu', 'Jamshedpur', 'Jodhpur', 'Kanpur', 'Kochi', 'Kolhapur',
        'Kolkata', 'Lucknow', 'Ludhiana', 'Madurai', 'Mangalore', 'Meerut', 'Mumbai', 'Mysuru',
        'Nagpur', 'Nashik', 'Patna', 'Pune', 'Raipur', 'Rajkot', 'Ranchi', 'Salem', 'Shimla',
        'Srinagar', 'Surat', 'Thiruvananthapuram', 'Tiruchirappalli', 'Udaipur', 'Vadodara', 'Varanasi', 'Vijayawada', 'Visakhapatnam'
    ]

    context = {'items': items, 'order': order, 'cartItems': cartItems, 'states': states, 'cities': cities}
    return render(request, 'store/checkout.html', context)


@login_required(login_url='login')
def account(request):
    customer = get_or_create_customer_for_user(request.user)
    orders = Order.objects.filter(customer=customer).order_by('-date_order')
    context = {'customer': customer, 'orders': orders, 'cartItems': cartData(request)['cartItems']}
    return render(request, 'store/account.html', context)


def login_view(request):
    if request.user.is_authenticated:
        return redirect('account')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            if user is not None:
                login(request, user)
                return redirect('account')
    else:
        form = AuthenticationForm()

    return render(request, 'store/login.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('store')


def register_view(request):
    if request.user.is_authenticated:
        return redirect('account')

    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            get_or_create_customer_for_user(user)
            login(request, user)
            return redirect('account')
    else:
        form = UserCreationForm()

    return render(request, 'store/register.html', {'form': form})


def updateItem(request):
    data = json.loads(request.body)
    productId = data['productId']
    action = data['action']

    customer = get_or_create_customer_for_user(request.user)
    product = Product.objects.get(id=productId)
    order, created = Order.objects.get_or_create(customer=customer, complete=False)

    orderItem, created = OrderItem.objects.get_or_create(order=order, product=product)

    if action == 'add':
        orderItem.quantity = orderItem.quantity + 1
    elif action == 'remove':
        orderItem.quantity = orderItem.quantity - 1

    orderItem.save()

    if orderItem.quantity <= 0:
        orderItem.delete()

    return JsonResponse('Item was added', safe=False)


def processOrder(request):
    transaction_id = datetime.datetime.now().timestamp()
    data = json.loads(request.body)

    if request.user.is_authenticated:
        customer = get_or_create_customer_for_user(request.user)
        order, created = Order.objects.get_or_create(customer=customer, complete=False)
    else:
        customer, order = guestOrder(request, data)

    total = float(data['form']['total'])
    order.transaction_id = str(transaction_id)

    if total == order.get_cart_total:
        order.complete = True
        order.status = 'Placed'

    order.save()

    if order.id:
        order.order_number = f'EC-{datetime.datetime.now().strftime("%Y%m%d")}-{order.id}'
        order.save(update_fields=['order_number'])

    if order.shipping == True:
        ShippingAddress.objects.create(
            customer=customer,
            order=order,
            address=data['shipping']['address'],
            city=data['shipping']['city'],
            state=data['shipping']['state'],
            zipcode=data['shipping']['zipcode'],
        )

    return JsonResponse('Payment submitted..', safe=False)


