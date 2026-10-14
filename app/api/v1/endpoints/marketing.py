from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.member_capabilities import DELETE_RESOURCES, member_can
from app.core.roles import PRODUCT_2, REVIEW_ACTION_ROLES
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.marketing import (
    MarketingArchiveTagResponse,
    MarketingContentDetailResponse,
    MarketingContentListResponse,
    MarketingContentResponse,
    MarketingRedlinePrepareResponse,
    MarketingReviewAnswers,
    MarketingReviewDecision,
    MarketingReviewResponse,
    MarketingUploadUrlRequest,
    MarketingUploadUrlResponse,
)
from app.services.entitlement_service import require_product
from app.services.marketing_service import (
    answer_marketing_review,
    create_marketing_upload_url,
    decide_marketing_review,
    delete_marketing_content,
    get_marketing_content,
    list_marketing_contents,
    prepare_marketing_review_redline,
    start_marketing_review_and_queue,
    tag_untagged_archive,
    upload_marketing_content,
)

router = APIRouter(prefix='/marketing', tags=['marketing'])


def _require_product_2(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> CurrentUser:
    from app.services.internal_org_service import is_reserved_internal_auth_org_id

    if is_reserved_internal_auth_org_id(user.org_id):
        return user
    require_product(db, auth_org_id=user.org_id, product_code=PRODUCT_2)
    return user


def _require_marketing_delete(user: CurrentUser = Depends(_require_product_2)) -> CurrentUser:
    if not member_can(user.role, DELETE_RESOURCES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master and CCO users can delete resources',
        )
    return user


def _require_cco_signoff(user: CurrentUser = Depends(_require_product_2)) -> CurrentUser:
    if not user.has_any_role(REVIEW_ACTION_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only a CCO or nominated reviewer can sign off on marketing options.',
        )
    return user


@router.get('/contents', response_model=MarketingContentListResponse)
def get_marketing_contents(
    user: CurrentUser = Depends(_require_product_2),
    db: Session | None = Depends(get_db),
) -> MarketingContentListResponse:
    return list_marketing_contents(user, db)


@router.post(
    '/contents/upload-url',
    response_model=MarketingUploadUrlResponse,
)
def post_marketing_content_upload_url(
    payload: MarketingUploadUrlRequest,
    user: CurrentUser = Depends(_require_product_2),
) -> MarketingUploadUrlResponse:
    return create_marketing_upload_url(
        user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
    )


@router.post(
    '/contents/upload',
    response_model=MarketingContentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_marketing_content_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile | None = File(default=None),
    title: str | None = Form(default=None),
    format: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    audience: str | None = Form(default=None),
    registrant_type: str | None = Form(default=None),
    channel: str | None = Form(default=None),
    body_text: str | None = Form(default=None),
    cadence_key: str | None = Form(default=None),
    cadence_step: int | None = Form(default=None),
    stored_path: str | None = Form(default=None),
    content_id: str | None = Form(default=None),
    user: CurrentUser = Depends(_require_product_2),
) -> MarketingContentResponse:
    return await upload_marketing_content(
        file,
        user,
        None,
        title=title,
        format_name=format,
        source_url=source_url,
        audience=audience,
        registrant_type=registrant_type,
        channel=channel,
        body_text=body_text,
        cadence_key=cadence_key,
        cadence_step=cadence_step,
        background_tasks=background_tasks,
        stored_path=stored_path,
        preferred_content_id=content_id,
        persist=True,
    )


@router.get('/contents/{content_id}', response_model=MarketingContentDetailResponse)
def get_one_marketing_content(
    content_id: str,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(_require_product_2),
    db: Session | None = Depends(get_db),
) -> MarketingContentDetailResponse:
    return get_marketing_content(content_id, user, db, background_tasks=background_tasks)


@router.delete('/contents/{content_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_one_marketing_content(
    content_id: str,
    user: CurrentUser = Depends(_require_marketing_delete),
    db: Session | None = Depends(get_db),
) -> None:
    delete_marketing_content(content_id, user, db)


@router.post(
    '/contents/{content_id}/review',
    response_model=MarketingReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_start_marketing_review(
    content_id: str,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(_require_product_2),
    db: Session | None = Depends(get_db),
) -> MarketingReviewResponse:
    return await start_marketing_review_and_queue(content_id, user, db, background_tasks)


@router.post('/reviews/{review_id}/answers', response_model=MarketingReviewResponse)
async def post_marketing_review_answers(
    review_id: str,
    payload: MarketingReviewAnswers,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(_require_product_2),
    db: Session | None = Depends(get_db),
) -> MarketingReviewResponse:
    return await answer_marketing_review(review_id, payload, user, db, background_tasks)


@router.put('/reviews/{review_id}/decision', response_model=MarketingReviewResponse)
def put_marketing_review_decision(
    review_id: str,
    payload: MarketingReviewDecision,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(_require_cco_signoff),
    db: Session | None = Depends(get_db),
) -> MarketingReviewResponse:
    return decide_marketing_review(review_id, payload, user, db, background_tasks)


@router.post(
    '/reviews/{review_id}/prepare-redline',
    response_model=MarketingRedlinePrepareResponse,
)
def post_prepare_marketing_review_redline(
    review_id: str,
    payload: MarketingReviewDecision,
    user: CurrentUser = Depends(_require_cco_signoff),
    db: Session | None = Depends(get_db),
) -> MarketingRedlinePrepareResponse:
    return prepare_marketing_review_redline(review_id, payload, user, db)


@router.post('/archive/tag', response_model=MarketingArchiveTagResponse)
def post_tag_marketing_archive(
    user: CurrentUser = Depends(_require_cco_signoff),
    db: Session | None = Depends(get_db),
) -> MarketingArchiveTagResponse:
    return tag_untagged_archive(user, db)
