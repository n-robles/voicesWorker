import subprocess
import os, uuid, pathlib
import shutil
import boto3
import time, datetime
import sendgrid
import newrelic.agent
from botocore.exceptions import ClientError
from sendgrid.helpers.mail import *

MAX_WORKERS = 1
PROJECT_ROOT = os.path.dirname( os.path.realpath( __file__ ) )
VOICES_ROOT = os.path.join( PROJECT_ROOT, 'temp' )
region = os.environ.get( 'AWS_DEFAULT_REGION' )

dynamodb = boto3.resource( 'dynamodb', region_name=region )
tabla_grabacion = dynamodb.Table( 'Grabacion' )

sqs = boto3.client( 'sqs' )
queue_url = os.environ.get( 'SQS_QUEUE_URL' )  # URL de SQS


@newrelic.agent.background_task(name='convertir_audio', group='Task')
def convertir_audio(fileImput):
    try:

        inFile = GetFileName( fileImput['Archivo_Original'] )  # Obtener solo nombre archivo
        name_ext = GetOrginalPath( fileImput['Archivo_Original'] )  # Obtiene nombre archivo y extension
        file_name_new = inFile + '.mp3'
        output_file = os.path.join( VOICES_ROOT, 'procesado', file_name_new )
        file_original = os.path.join( VOICES_ROOT, 'original', name_ext )
        s3_name = 'procesado/' + file_name_new

        s3 = boto3.resource( 's3' )
        s3.Bucket( 'supervoices-app' ).download_file( fileImput['Archivo_Original'], file_original )

        upload_file_s3 = False

        if IsMp3( file_original ):
            shutil.copy( file_original, output_file )
            upload_file_s3 = True
        else:
            result = subprocess.run(['ffmpeg', '-i', file_original, '-acodec', 'libmp3lame', output_file])
            #result = subprocess.run(['C:\\ffmpeg-20190403\\bin\\ffmpeg.exe', '-i', file_original, '-acodec', 'libmp3lame', output_file] )
            if result.returncode is 0:
                upload_file_s3 = True

        # Subir archivo a S3
        if (upload_file_s3):
            s4 = boto3.client('s3')
            s4.upload_file(output_file, 'supervoices-app', s3_name)
            os.remove( output_file )
            procesar_archivo(fileImput['id'], s3_name, fileImput['Mail_Autor'], fileImput['Concurso_id'])

        os.remove( file_original )

    except ClientError as error:
        print( error )
    except Exception as ex:
        print( ex )


def procesar_archivo(fileId, outFile, mail, concurso_id):
    try:
        actualizar_grabacion( fileId, outFile )

        # Buscar URL Del Concurso, no existen relaciones en dynamodb
        tabla_concurso = dynamodb.Table( 'Concurso' )
        respuesta_concurso = tabla_concurso.get_item(
            Key={
                'id': concurso_id
            }
        )
        concurso = respuesta_concurso['Item']
        url = concurso['Url_Concurso']
        SendEmailSendgrid( mail, url )
    except Exception as ex:
        print( ex )


def GetOrginalPath(relativepath):
    file = pathlib.PurePath( relativepath ).name
    return file


def IsMp3(filePath):
    file_extension = pathlib.PurePath( filePath ).suffix
    if file_extension == '.mp3':
        return True
    else:
        return False


def GetFileName(filePath):
    return pathlib.PurePath( filePath ).stem


def actualizar_grabacion(fileId, filePath):
    try:
        # Actualizar datos en dynamodb
        respuesta = tabla_grabacion.update_item(
            Key={
                'id': fileId
            },
            UpdateExpression=" SET Estado_Archivo = :EstArchivo, Archivo_Final=:ArcFinal ",
            ExpressionAttributeValues={
                ':EstArchivo': '1',
                ':ArcFinal': filePath
            },
            ReturnValues="UPDATED_NEW"
        )

        eliminar_mensaje()
        print( respuesta )
    except(ClientError) as error:
        print( error )
    except Exception as ex:
        print( ex )


@newrelic.agent.background_task(name='SendEmailSendgrid', group='Task')
def SendEmailSendgrid(email, url):
    try:
        print("usendmail--" + email)
        sg = sendgrid.SendGridAPIClient(
            api_key=os.environ.get('SENDGRID_API_KEY')
        )
        from_email = "noreply@voices.com"
        subject = "La voz ya esta disponible"
        WS_IP = os.environ.get( 'IP_HOST' ) + '/concursar/' + url
        content = '<html><head></head><body><p> Heroku APP . La voz ya se encuentra disponible en la página principal del ' + \
                  'concurso, visite</p> <a href="' + WS_IP + '">Supervoices</a> ' + \
                  '<p>para mas informacion</p></body></html>'

        message = Mail(from_email=from_email, to_emails=email, subject=subject, html_content=content)
        response = sg.send(message)
        print( response.status_code )
    except Exception as ex:
        print( ex )

def get_grabacion(id):
    try:
        respuesta_grabacion = tabla_grabacion.get_item(
            Key={
                'id': id
            }
        )

        grabacion = respuesta_grabacion['Item']
        fecha_inicio = datetime.datetime.now().strftime( '%Y-%m-%d %H:%M:%S.%f')
        convertir_audio(grabacion)
        fecha_fin  = datetime.datetime.now().strftime( '%Y-%m-%d %H:%M:%S.%f')

        UpdateLogTable(fecha_inicio, fecha_fin, 1)

    except ClientError as error:
        print(error)
    except Exception as ex:
        print(ex)


@newrelic.agent.background_task(name='UpdateLogTable', group='Task')
def UpdateLogTable(startTime, endTime, totalFiles):
    try:
        id = str( uuid.uuid4() )
        tabla_log = dynamodb.Table( 'BatchLog' )

        respuesta = tabla_log.put_item(
            Item={
                'Fecha_Inicio': startTime,
                'Fecha_Fin': endTime,
                'Total_Archivos': totalFiles,
                'id': id
            }
        )
        print( respuesta )
    except(ClientError) as error:
        print( error )


@newrelic.agent.background_task(name='leer_mensaje', group='Task')
def leer_mensaje():

    # Validar directorios temporales

    file_original = os.path.join( VOICES_ROOT, 'original')
    if not os.path.exists(file_original):
        os.makedirs( file_original )

    file_procesado = os.path.join( VOICES_ROOT, 'procesado' )
    if not os.path.exists(file_procesado):
        os.makedirs( file_procesado )


    while True:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            AttributeNames=[
                'SentTimestamp'
            ],
            MaxNumberOfMessages=1,
            MessageAttributeNames=[
                'All'
            ],
            VisibilityTimeout=120,
            WaitTimeSeconds=20
        )

        if (len(response) > 1):
            message = response['Messages'][0]
            global receipt_handle
            receipt_handle = message['ReceiptHandle']
            #eliminar_mensaje()
            atributos = message['MessageAttributes']
            get_grabacion(atributos['Grabacion_id']['StringValue'])
        else:
            time.sleep(15)


@newrelic.agent.background_task(name='eliminar_mensaje', group='Task')
def eliminar_mensaje():
    sqs.delete_message(
        QueueUrl=queue_url,
        ReceiptHandle=receipt_handle
    )


if __name__ == '__main__':
    leer_mensaje()
