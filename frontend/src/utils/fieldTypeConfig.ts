import { valueTypeLabel, valueTypeOptions } from "@/constants/labels";

type FieldTypeConfig = {
  value_type: string;
  extraction_strategy?: "RAW_TEXT" | "DETERMINISTIC" | "STRUCTURED" | null;
};

export const STRUCTURED_FIELD_TYPE = "STRUCTURED";
export const fieldDataTypeOptions = [...valueTypeOptions, { value: STRUCTURED_FIELD_TYPE, label: "结构化对象" }];

export function fieldDataType(field: FieldTypeConfig): string {
  return field.extraction_strategy === "STRUCTURED" ? STRUCTURED_FIELD_TYPE : field.value_type;
}

export function fieldDataTypeLabel(field: FieldTypeConfig): string {
  return fieldDataType(field) === STRUCTURED_FIELD_TYPE ? "结构化对象" : valueTypeLabel(field.value_type);
}

/** 结构化对象在表单中作为数据类型，接口仍使用已有的类型和策略字段。 */
export function fieldTypePayload(dataType: string, previous?: FieldTypeConfig | null) {
  if (dataType === STRUCTURED_FIELD_TYPE) {
    return {
      value_type: previous?.extraction_strategy === "STRUCTURED" ? previous.value_type : "STRING",
      extraction_strategy: "STRUCTURED" as const,
    };
  }
  const defaultStrategy = ["STRING", "CLAUSE", "DECIMAL_CN"].includes(dataType) ? "RAW_TEXT" : "DETERMINISTIC";
  const previousStrategy = previous?.value_type === dataType ? previous.extraction_strategy : null;
  return {
    value_type: dataType,
    extraction_strategy: previousStrategy && previousStrategy !== "STRUCTURED" ? previousStrategy : defaultStrategy,
  };
}
