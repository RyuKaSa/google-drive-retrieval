from django.urls import path
from . import views


app_name = "driveapp"

urlpatterns = [
    path('', views.index, name='index'),
    path('login/', views.login, name='login'),
    path('oauth2callback/', views.oauth2callback, name='oauth2callback'),
    path('metadata/', views.metadata, name='metadata'),  # raw JSON catcher
    path('fetch/', views.fetch_and_download, name='fetch'),
    path('search/', views.search_drive, name='search'),
]
