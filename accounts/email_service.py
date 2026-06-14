import os
import logging
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from smtplib import SMTPDataError

logger = logging.getLogger(__name__)


def send_email(
    to_email,
    subject,
    template_name,
    context=None,
    attachments=None
):
    """
    Send email using Amazon SES SMTP via Django's email backend.
    
    Args:
        to_email: Recipient email address
        subject: Email subject
        template_name: Name of the email template (without 'email_templates/' prefix)
        context: Dictionary of context variables for template rendering
    
    Returns:
        int: 200 on success, None on failure
    """
    # Log function call
    logger.info(f"Attempting to send email to {to_email} with subject: {subject}")
    
    if not to_email:
        logger.error("Recipient email is required but not provided")
        raise ValueError("Recipient email is required")
    
    # Validate email configuration
    # if not settings.EMAIL_HOST:
    #     logger.error("EMAIL_HOST is not configured. Check AWS_SES_SMTP_HOST environment variable.")
    #     print("ERROR: EMAIL_HOST is not configured. Check AWS_SES_SMTP_HOST environment variable.")
    #     return None
    
    # if not settings.EMAIL_HOST_USER:
    #     logger.error("EMAIL_HOST_USER is not configured. Check AWS_SES_SMTP_USERNAME environment variable.")
    #     print("ERROR: EMAIL_HOST_USER is not configured. Check AWS_SES_SMTP_USERNAME environment variable.")
    #     return None
    
    # if not settings.DEFAULT_FROM_EMAIL:
    #     logger.error("DEFAULT_FROM_EMAIL is not configured. Check AWS_SES_DEFAULT_FROM_EMAIL environment variable.")
    #     print("ERROR: DEFAULT_FROM_EMAIL is not configured. Check AWS_SES_DEFAULT_FROM_EMAIL environment variable.")
    #     return None
    
    context = context or {}
    
    try:
        html_content = render_to_string(
            f"email_templates/{template_name}",
            context
        )
        logger.debug(f"Email template '{template_name}' rendered successfully")
    except Exception as e:
        logger.error(f"Failed to render email template '{template_name}': {str(e)}")
        print(f"ERROR: Failed to render email template '{template_name}': {str(e)}")
        return None

    try:
        email = EmailMessage(
            subject=subject,
            body=html_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
        )
        email.content_subtype = 'html'  # Set content type to HTML

        if attachments:
            for attachment in attachments:
                try:
                    email.attach(
                        attachment.get("filename"),
                        attachment.get("content"),
                        attachment.get("mimetype")
                    )
                except Exception as attach_err:
                    logger.error(f"Failed to attach file {attachment.get('filename')}: {str(attach_err)}")
        
        logger.info(f"Sending email via Amazon SES SMTP (Host: {settings.EMAIL_HOST}, Port: {settings.EMAIL_PORT})")
        print(f"INFO: Sending email to {to_email} via Amazon SES {settings.EMAIL_HOST}:{settings.EMAIL_PORT}")
        
        # Send email via Django's SMTP backend (configured for Amazon SES)
        result = email.send()
        
        if result:
            logger.info(f"Email sent successfully to {to_email} with subject: {subject}")
            print(f"SUCCESS: Email sent to {to_email}")
            return 200  # Return 200 on success
        else:
            logger.warning(f"Email sending returned False for {to_email}")
            print(f"WARNING: Email sending returned False for {to_email}")
            return None
            
    except SMTPDataError as e:
        error_msg = str(e)
        # SMTPDataError has args[0] as error code and args[1] as error message
        error_code = e.args[0] if e.args and len(e.args) > 0 else None
        error_response = e.args[1] if e.args and len(e.args) > 1 else None
        
        # Check for AWS SES sandbox mode error (554 - email not verified)
        if error_code == 554 and error_response and 'not verified' in str(error_response).lower():
            logger.error(
                f"AWS SES Sandbox Mode Error: Email address '{to_email}' is not verified. "
                f"Either verify this email in AWS SES Console or request production access. "
                f"Error: {error_msg}"
            )
            print(
                f"ERROR: AWS SES Sandbox Mode - Email '{to_email}' is not verified. "
                f"Verify the email in AWS SES Console or request production access."
            )
        else:
            logger.error(f"SMTP Error ({error_code}) sending email to {to_email}: {error_msg}", exc_info=True)
            print(f"ERROR: SMTP Error ({error_code}) - Failed to send email to {to_email}: {error_msg}")
        return None
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to send email to {to_email}: {error_msg}", exc_info=True)
        print(f"ERROR: Failed to send email to {to_email}: {error_msg}")
        return None
