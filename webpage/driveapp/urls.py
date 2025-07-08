from django.urls import path
from . import views
from .views import fetch_and_download


app_name = "driveapp"

urlpatterns = [
    path('', views.index, name='index'),
    path('login/', views.login, name='login'),
    path('oauth2callback/', views.oauth2callback, name='oauth2callback'),
    path('metadata/', views.metadata, name='metadata'),  # raw JSON catcher
    path('fetch/', fetch_and_download, name='fetch'),
]
