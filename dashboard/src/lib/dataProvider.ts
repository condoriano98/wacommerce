import type { DataProvider } from "@refinedev/core";
import { omsFetch } from "./api";

// Resources map 1:1 to OMS read endpoints; row visibility is enforced by PostgreSQL RLS behind the API.
export const dataProvider: DataProvider = {
  getApiUrl: () => "",
  getList: async ({ resource }) => {
    const data = await omsFetch(`/${resource}`);
    return { data: data ?? [], total: Array.isArray(data) ? data.length : 0 };
  },
  getOne: async () => {
    throw { statusCode: 501, message: "not supported" };
  },
  create: async ({ resource, variables }) => ({
    data: await omsFetch(`/${resource}`, { method: "POST", body: JSON.stringify(variables) }),
  }),
  update: async () => {
    throw { statusCode: 501, message: "not supported" };
  },
  deleteOne: async () => {
    throw { statusCode: 501, message: "not supported" };
  },
  custom: async ({ url, method, payload }) => ({
    data: await omsFetch(url, { method: method.toUpperCase(), body: payload ? JSON.stringify(payload) : undefined }),
  }),
};
