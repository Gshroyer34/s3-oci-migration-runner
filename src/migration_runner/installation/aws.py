"""Generate a CloudFormation source-reader package; never owns the source bucket."""
from .profile import InstallationProfile


def reader_template(profile: InstallationProfile):
    source = profile.source
    bucket = f'arn:aws:s3:::{source.bucket}'
    path = f'{bucket}/{source.prefix}*'
    via_endpoint = ({'StringEquals': {'aws:SourceVpce': profile.network.s3_endpoint_id}}
                    if profile.network.payload_route == 'private' else {})
    listing = {'StringLike': {'s3:prefix': [source.prefix, source.prefix + '*']}, **via_endpoint}
    statements = [
        {'Effect': 'Allow', 'Action': ['s3:GetBucketLocation'], 'Resource': bucket, **({'Condition': via_endpoint} if via_endpoint else {})},
        {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': bucket, 'Condition': listing},
        {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:GetObjectVersion', 's3:GetObjectTagging', 's3:GetObjectAcl', 's3:GetObjectVersionAcl'], 'Resource': path,
         **({'Condition': via_endpoint} if via_endpoint else {})},
    ]
    if source.kms_key_arn:
        statements.append({'Effect': 'Allow', 'Action': ['kms:Decrypt'], 'Resource': source.kms_key_arn,
                           'Condition': {'StringEquals': {'kms:ViaService': f's3.{source.region}.amazonaws.com'},
                                         'StringLike': {'kms:EncryptionContext:aws:s3:arn': [bucket, path]}}})
    policy = {'Version': '2012-10-17', 'Statement': statements}
    return {
        'AWSTemplateFormatVersion': '2010-09-09',
        'Description': 'Migration source read access only. Existing buckets and keys are not managed by this stack.',
        'Parameters': {'PublicCaCertificate': {'Type': 'String', 'MinLength': 64, 'MaxLength': 8000,
                        'AllowedPattern': r'(?s)^-----BEGIN CERTIFICATE-----.*-----END CERTIFICATE-----\s*$',
                        'Description': 'Deployment-owned public X.509 CA certificate. Never enter a private key.'}},
        'Rules': {'IntendedEnvironment': {'Assertions': [
            {'Assert': {'Fn::Equals': [{'Ref': 'AWS::AccountId'}, source.account_id]}, 'AssertDescription': 'Use the configured source AWS account.'},
            {'Assert': {'Fn::Equals': [{'Ref': 'AWS::Region'}, source.region]}, 'AssertDescription': 'Use the configured source AWS region.'},
        ]}},
        'Resources': {
            'TrustAnchor': {'Type': 'AWS::RolesAnywhere::TrustAnchor', 'Properties': {
                'Name': {'Fn::Sub': '${AWS::StackName}-trust'}, 'Enabled': True,
                'Source': {'SourceType': 'CERTIFICATE_BUNDLE', 'SourceData': {'X509CertificateData': {'Ref': 'PublicCaCertificate'}}}}},
            'ReaderRole': {'Type': 'AWS::IAM::Role', 'Properties': {
                'MaxSessionDuration': 3600,
                'AssumeRolePolicyDocument': {'Version': '2012-10-17', 'Statement': [{
                    'Effect': 'Allow', 'Principal': {'Service': 'rolesanywhere.amazonaws.com'},
                    'Action': ['sts:AssumeRole', 'sts:TagSession', 'sts:SetSourceIdentity'],
                    'Condition': {'ArnEquals': {'aws:SourceArn': {'Fn::GetAtt': ['TrustAnchor', 'TrustAnchorArn']}},
                                  'StringEquals': {'aws:SourceAccount': source.account_id,
                                                   'aws:PrincipalTag/x509Subject/CN': f'migration-{profile.deployment_name}-worker'}}}]},
                'Policies': [{'PolicyName': 'approved-source-read', 'PolicyDocument': policy}]}},
            'ReaderProfile': {'Type': 'AWS::RolesAnywhere::Profile', 'Properties': {
                'Name': {'Fn::Sub': '${AWS::StackName}-reader'}, 'Enabled': True, 'DurationSeconds': 900,
                'RoleArns': [{'Fn::GetAtt': ['ReaderRole', 'Arn']}]}}
        },
        'Outputs': {
            'ReaderRoleArn': {'Value': {'Fn::GetAtt': ['ReaderRole', 'Arn']}},
            'TrustAnchorArn': {'Value': {'Fn::GetAtt': ['TrustAnchor', 'TrustAnchorArn']}},
            'ReaderProfileArn': {'Value': {'Fn::GetAtt': ['ReaderProfile', 'ProfileArn']}},
            'CertificateCommonName': {'Value': f'migration-{profile.deployment_name}-worker'},
            'ConfigurationRevision': {'Value': profile.revision},
        },
    }
