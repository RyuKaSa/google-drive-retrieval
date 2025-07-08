# driveapp/views.py

import os
import json
import io

from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import (
    JsonResponse,
    HttpResponseBadRequest,
    HttpResponseNotAllowed,
)
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']


def get_flow(request):
    redirect_uri = request.build_absolute_uri(
        reverse('driveapp:oauth2callback')
    )
    return Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def index(request):
    creds = request.session.get('credentials')
    return render(request, 'index.html', {
        'creds': creds,
        'api_key': os.getenv('GOOGLE_API_KEY'),
        'client_id': os.getenv('GOOGLE_CLIENT_ID'),
        'allowed_mimes': json.dumps(settings.DRIVE_ALLOWED_MIME_TYPES),
    })


def login(request):
    flow = get_flow(request)
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
    )
    return redirect(auth_url)


def oauth2callback(request):
    try:
        flow = get_flow(request)
        flow.fetch_token(authorization_response=request.build_absolute_uri())
        creds = flow.credentials

        # Safely grab token_uri from client_config
        cfg = (flow.client_config.get('web')
               or flow.client_config.get('installed')
               or {})
        token_uri = cfg.get(
            'token_uri',
            'https://oauth2.googleapis.com/token'
        )

        request.session['credentials'] = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes,
        }
        return redirect('driveapp:index')

    except Exception as e:
        return HttpResponseBadRequest(f"OAuth failed: {e}".encode('utf-8'))


def get_drive_service(request):
    creds_data = request.session.get('credentials')
    if not creds_data:
        return None

    creds = Credentials(**creds_data)
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            request.session['credentials']['token'] = creds.token
        return build('drive', 'v3', credentials=creds)

    except RefreshError:
        del request.session['credentials']
        return None
    except Exception:
        return None


@csrf_exempt
def metadata(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    try:
        payload = request.body.decode('utf-8')
        docs    = json.loads(payload).get('docs', [])
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'bad json'}, status=400)
    return JsonResponse(docs, safe=False)


@csrf_exempt
def fetch_and_download(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    try:
        payload = json.loads(request.body.decode('utf-8'))
        docs    = payload.get('docs', [])
    except (ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid JSON payload'.encode('utf-8'))

    service = get_drive_service(request)
    if not service:
        return HttpResponseBadRequest('Missing or expired Drive credentials'.encode('utf-8'))

    allowed_mimes = set(settings.DRIVE_ALLOWED_MIME_TYPES)
    downloaded   = []

    def recurse(items):
        for item in items:
            fid, name, mime = (
                item.get('id'),
                item.get('name'),
                item.get('mimeType'),
            )

            if mime in allowed_mimes:
                # download file to project root
                local_dir = os.path.join(settings.BASE_DIR, 'downloads')
                request_media = service.files().get_media(fileId=fid)
                fh            = io.BytesIO()
                downloader    = MediaIoBaseDownload(fh, request_media)

                done = False
                while not done:
                    status, done = downloader.next_chunk()

                with open(os.path.join(local_dir, name), 'wb') as out:
                    
                    out.write(fh.getvalue())

                downloaded.append(name)

            elif mime == 'application/vnd.google-apps.folder':
                # list children and recurse
                page_token = None
                children   = []
                while True:
                    resp = service.files().list(
                        q=f"'{fid}' in parents and trashed=false",
                        fields="nextPageToken, files(id,name,mimeType)",
                        pageToken=page_token
                    ).execute()
                    children.extend(resp.get('files', []))
                    page_token = resp.get('nextPageToken')
                    if not page_token:
                        break

                recurse(children)
            # otherwise skip

    recurse(docs)

    return JsonResponse({'downloaded': downloaded}, status=200 if downloaded else 500)

